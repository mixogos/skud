"""Видеоаналитика камеры: счётчики лиц в кадре и пересечение линий.

Работает внутри потока камеры: на каждом обработанном кадре получает найденные лица
и живые треки. Настройки читаются из базы при подключении и после каждой правки.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import cv2
import numpy as np
from sqlalchemy import delete, select, update

from .. import models, storage
from ..db import SessionLocal
from ..notifications import broadcaster
from ..recognition import engine as eng
from ..recognition.engine import Face
from .tracker import Track

log = logging.getLogger("skud.analytics")

RECORD_IMAGE_SIDE = 640
RECORD_IMAGES_KEPT = 500  # снимков на счётчик; у более старых записей остаётся только число
RECORD_DAYS_KEPT = 30
RECROSS_PAUSE = 1.0  # секунд: повторное пересечение той же линии тем же человеком не считается


def _center(bbox, width: int, height: int) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2 / width, (bbox[1] + bbox[3]) / 2 / height


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def crossing_direction(points, before: tuple[float, float], after: tuple[float, float]) -> str | None:
    """Пересёк ли отрезок движения before → after линию. Возвращает "AB", "BA" или None.

    Сторона A — слева, если смотреть от первой точки линии ко второй; B — справа.
    """
    (x1, y1), (x2, y2) = points
    dx, dy = x2 - x1, y2 - y1
    side_before = _cross(dx, dy, before[0] - x1, before[1] - y1) > 0
    side_after = _cross(dx, dy, after[0] - x1, after[1] - y1) > 0
    if side_before == side_after:
        return None
    # Концы линии должны лежать по разные стороны от пути человека — иначе он прошёл мимо отрезка.
    mx, my = after[0] - before[0], after[1] - before[1]
    if _cross(mx, my, x1 - before[0], y1 - before[1]) * _cross(mx, my, x2 - before[0], y2 - before[1]) > 0:
        return None
    return "AB" if side_after else "BA"


def in_roi(roi, point: tuple[float, float]) -> bool:
    if not roi:
        return True
    left, top, right, bottom = roi
    return left <= point[0] <= right and top <= point[1] <= bottom


@dataclass
class _Counter:
    id: int
    interval: float
    roi: list
    last: float = -1e9
    cleaned: float = -1e9


@dataclass
class _Line:
    id: int
    points: list


@dataclass
class CameraAnalytics:
    camera_id: int
    dirty: bool = True  # настройки изменились — перечитать из базы
    counters: list[_Counter] = field(default_factory=list)
    lines: list[_Line] = field(default_factory=list)
    # (номер трека, линия) → время последнего пересечения; номер трека → записи о пересечениях
    _crossed_at: dict = field(default_factory=dict)
    _track_crossings: dict = field(default_factory=dict)

    def _reload(self) -> None:
        self.dirty = False
        previous = {c.id: c for c in self.counters}
        with SessionLocal() as db:
            counters = db.scalars(
                select(models.Counter).where(models.Counter.camera_id == self.camera_id, models.Counter.active)
            ).all()
            lines = db.scalars(select(models.Line).where(models.Line.camera_id == self.camera_id, models.Line.active)).all()
            self.counters = [_Counter(c.id, float(c.count_interval), list(c.roi or [])) for c in counters]
            self.lines = [_Line(line.id, line.points) for line in lines if len(line.points or []) == 2]
        for counter in self.counters:
            if counter.id in previous:
                counter.last, counter.cleaned = previous[counter.id].last, previous[counter.id].cleaned

    def new_session(self) -> None:
        """Новое подключение к камере: номера треков начинаются заново."""
        self._crossed_at.clear()
        self._track_crossings.clear()
        for counter in self.counters:
            counter.last = -1e9  # отсчёт времени потока тоже начинается заново

    # --- Каждый кадр ---
    def on_frame(self, frame: np.ndarray, faces: list[Face], tracks: list[Track], seconds: float, when: datetime) -> None:
        if self.dirty:
            self._reload()
        if not self.counters and not self.lines:
            return
        height, width = frame.shape[:2]
        try:
            for counter in self.counters:
                if seconds - counter.last >= counter.interval:
                    counter.last = seconds
                    self._count(counter, frame, faces, when, width, height)
            if self.lines:
                self._check_lines(tracks, seconds, when, width, height)
        except Exception:  # аналитика не должна останавливать распознавание
            log.exception("Сбой аналитики камеры %s", self.camera_id)

    def _count(self, counter: _Counter, frame: np.ndarray, faces: list[Face], when: datetime, width: int, height: int) -> None:
        inside = [f.bbox for f in faces if in_roi(counter.roi, _center(f.bbox, width, height))]
        boxes = [[round(b[0] / width, 4), round(b[1] / height, 4), round(b[2] / width, 4), round(b[3] / height, 4)] for b in inside]
        with SessionLocal() as db:
            if db.get(models.Counter, counter.id) is None:
                self.dirty = True
                return
            record = models.CounterRecord(
                counter_id=counter.id, created_date=when, faces_count=len(inside), faces_bbox=boxes, has_image=bool(inside)
            )
            db.add(record)
            db.flush()
            if inside:  # снимок храним только когда в кадре кто-то есть
                scale = RECORD_IMAGE_SIDE / max(width, height)
                image = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame
                storage.save_counter_image(record.id, eng.encode_jpeg(image, 75))
            if time.monotonic() - counter.cleaned > 600:
                counter.cleaned = time.monotonic()
                cleanup_records(db, counter.id, when)
            db.commit()
            broadcaster.publish("counter_record_created", {
                "id": record.id, "counter_id": counter.id, "created_date": when.isoformat(), "faces_count": len(inside),
            })

    def _check_lines(self, tracks: list[Track], seconds: float, when: datetime, width: int, height: int) -> None:
        found = []
        for track in tracks:
            if track.prev_bbox is None or track.last_time != seconds:
                continue  # трек на этом кадре не двигался
            before, after = _center(track.prev_bbox, width, height), _center(track.bbox, width, height)
            for line in self.lines:
                direction = crossing_direction(line.points, before, after)
                if direction is None or seconds - self._crossed_at.get((track.id, line.id), -1e9) < RECROSS_PAUSE:
                    continue
                self._crossed_at[(track.id, line.id)] = seconds
                found.append((track.id, line.id, direction))
        if not found:
            return
        with SessionLocal() as db:
            known = set(db.scalars(select(models.Line.id).where(models.Line.id.in_({f[1] for f in found}))))
            rows = []
            for track_id, line_id, direction in found:
                if line_id not in known:
                    self.dirty = True
                    continue
                row = models.LineCrossing(line_id=line_id, created_date=when, direction=direction)
                db.add(row)
                rows.append((track_id, row))
            db.commit()
            for track_id, row in rows:
                self._track_crossings.setdefault(track_id, []).append(row.id)
                broadcaster.publish("line_crossing_created", {
                    "id": row.id, "line_id": row.line_id, "created_date": when.isoformat(), "direction": row.direction,
                })

    # --- Трек завершился ---
    def on_track_finished(self, track: Track, successor_id: int | None, event_id: int | None, card_id: int | None) -> None:
        """Дописывает к пересечениям трека, кто это был. Вызывается для каждого завершённого трека."""
        crossing_ids = self._track_crossings.pop(track.id, [])
        for key in [k for k in self._crossed_at if k[0] == track.id]:
            moment = self._crossed_at.pop(key)
            if successor_id is not None:  # трек разрезан по времени: человек тот же
                self._crossed_at[(successor_id, key[1])] = moment
        if not crossing_ids or (event_id is None and card_id is None):
            return
        with SessionLocal() as db:
            db.execute(
                update(models.LineCrossing).where(models.LineCrossing.id.in_(crossing_ids))
                .values(face_event_id=event_id, matched_card_id=card_id)
            )
            db.commit()


def cleanup_records(db, counter_id: int, now: datetime) -> None:
    """Удаляет старые снимки и записи счётчика. Фиксация транзакции — на вызывающем."""
    R = models.CounterRecord
    old_images = db.scalars(
        select(R.id).where(R.counter_id == counter_id, R.has_image).order_by(R.id.desc()).offset(RECORD_IMAGES_KEPT)
    ).all()
    for record_id in old_images:
        storage.delete_counter_image(record_id)
    for start in range(0, len(old_images), 500):
        db.execute(update(R).where(R.id.in_(old_images[start:start + 500])).values(has_image=False))
    db.execute(delete(R).where(R.counter_id == counter_id, R.created_date < now - timedelta(days=RECORD_DAYS_KEPT)))


def delete_counter_images(db, *conditions) -> None:
    """Удаляет файлы снимков у записей по условию (перед удалением самих записей)."""
    R = models.CounterRecord
    for record_id in db.scalars(select(R.id).where(R.has_image, *conditions)):
        storage.delete_counter_image(record_id)

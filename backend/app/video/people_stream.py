"""Детекция и трекинг людей в потоке камеры: силуэты, атрибуты, события по людям.

Работает параллельно с распознаванием лиц, но реже (детектор людей тяжелее): на каждого
прошедшего человека создаётся одно событие с лучшим кадром, полом, одеждой и аксессуарами.
Включается в настройках группы камер (detect_people).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import delete, select

from .. import models, storage
from ..db import SessionLocal
from ..notifications import broadcaster
from ..recognition import engine as eng
from ..recognition.people import PersonBox, get_people

log = logging.getLogger("skud.people")

DETECT_INTERVAL = 0.5   # как часто искать людей, секунд (детектор тяжёлый — реже, чем лица)
MIN_TRACK_FRAMES = 2    # минимум обнаружений, чтобы создать событие (отсекает ложные)
MAX_TRACK_GAP = 1.5     # через сколько секунд без человека трек завершается
IOU_MIN = 0.3
DIST_MAX = 0.8  # для быстрого движения: сопоставляем трек и рамку по близости центров
# Минимальное смещение за время трека в долях роста силуэта: неподвижные «люди» — это
# картины, статуи, плакаты и манекены; настоящий человек за проход заметно перемещается.
MOVE_MIN = 0.2

# Карта «насиженных» мест (занятость по центрам силуэтов). На фиксированной камере картины,
# статуи, плакаты и манекены детектор видит как людей в одной и той же точке в каждом проходе.
# Копим по сетке долю проходов, в которых центр какого-нибудь силуэта попадал в ячейку: у
# обстановки центр почти всегда в одной точке (доля высокая), у проходящего посетителя центр
# всё время смещается (в каждой ячейке он мельком — доля низкая). Накопление по центру, а не
# по площади рамки, не размазывается по дорожкам, где просто часто ходят люди, и устойчиво к
# дрожанию размеров рамки (детектор на картинах заметно «гуляет»). Считаем место обстановкой,
# если силуэт держится там И при этом почти не перемещается за время трека.
STATIC_GRID_W = 48      # сетка по ширине кадра
STATIC_GRID_H = 27      # и по высоте
STATIC_DECAY = 0.96     # инерция карты: чем ближе к 1, тем дольше помнит занятость
STATIC_OCC_MIN = 0.28   # доля проходов с центром в точке, выше которой место «насиженное»
STATIC_WARMUP = 20      # первые проходы карта ещё набирается — пока не судим


def _iou(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def _center_dist(a, b) -> float:
    """Расстояние между центрами в долях размера большей рамки."""
    ax, ay = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    bx, by = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    size = max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1], 1)
    return float(np.hypot(ax - bx, ay - by)) / size


@dataclass
class _Track:
    id: int
    first_time: float
    last_time: float
    bbox: tuple
    frames: int = 1
    best_area: float = -1.0
    best_bbox: tuple | None = None
    best_frame: np.ndarray | None = field(default=None, repr=False)
    best_score: float = 0.0
    start_center: tuple | None = None
    max_move: float = 0.0

    def offer(self, frame: np.ndarray, box: PersonBox) -> None:
        left, top, right, bottom = box.bbox
        area = (right - left) * (bottom - top)
        if area > self.best_area:  # самый крупный кадр — лучший для атрибутов
            self.best_area, self.best_bbox, self.best_frame, self.best_score = area, box.bbox, frame, box.score

    def note_position(self) -> None:
        """Запоминает смещение центра силуэта относительно первого появления."""
        cx, cy = (self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2
        if self.start_center is None:
            self.start_center = (cx, cy)
        else:
            self.max_move = max(self.max_move, float(np.hypot(cx - self.start_center[0], cy - self.start_center[1])))

    @property
    def duration(self) -> float:
        return self.last_time - self.first_time

    @property
    def moved_enough(self) -> bool:
        height = max(1, (self.best_bbox or self.bbox)[3] - (self.best_bbox or self.bbox)[1])
        return self.max_move >= MOVE_MIN * height


class PeopleTracker:
    def __init__(self):
        self._tracks: list[_Track] = []
        self._next_id = 1

    def update(self, frame: np.ndarray, boxes: list[PersonBox], time: float) -> list[_Track]:
        free_tracks = list(range(len(self._tracks)))
        free_boxes = list(range(len(boxes)))

        def assign(score_fn, accept) -> None:
            pairs = sorted(
                ((score_fn(self._tracks[t].bbox, boxes[i].bbox), t, i) for t in free_tracks for i in free_boxes),
                reverse=True,
            )
            for score, t, i in pairs:
                if accept(score) and t in free_tracks and i in free_boxes:
                    track, box = self._tracks[t], boxes[i]
                    track.bbox, track.last_time, track.frames = box.bbox, time, track.frames + 1
                    track.offer(frame, box)
                    track.note_position()
                    free_tracks.remove(t)
                    free_boxes.remove(i)

        # Сначала по пересечению рамок, затем — для быстрого движения — по близости центров.
        assign(_iou, lambda s: s >= IOU_MIN)
        assign(lambda a, b: -_center_dist(a, b), lambda s: -s <= DIST_MAX)
        for i in free_boxes:
            track = _Track(self._next_id, time, time, boxes[i].bbox)
            track.offer(frame, boxes[i])
            track.note_position()
            self._next_id += 1
            self._tracks.append(track)
        finished, alive = [], []
        for track in self._tracks:
            (finished if time - track.last_time > MAX_TRACK_GAP else alive).append(track)
        self._tracks = alive
        return finished

    def flush(self) -> list[_Track]:
        finished, self._tracks = self._tracks, []
        return finished


class StaticScene:
    """Карта «насиженных» мест кадра: куда центр силуэта попадает почти в каждом проходе —
    там висит/стоит обстановка (картина, статуя, плакат, манекен), а не идёт посетитель.
    Накопление по центру устойчиво к дрожанию размеров рамки и не размазывается по дорожкам."""

    def __init__(self):
        self._occ: np.ndarray | None = None  # доля проходов с центром силуэта в ячейке
        self._passes = 0
        self._w = self._h = 0

    def _center_cell(self, bbox) -> tuple[int, int]:
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        c = min(STATIC_GRID_W - 1, max(0, int(cx / self._w * STATIC_GRID_W)))
        r = min(STATIC_GRID_H - 1, max(0, int(cy / self._h * STATIC_GRID_H)))
        return r, c

    def observe(self, boxes: list[PersonBox], width: int, height: int) -> None:
        if self._occ is None or self._w != width or self._h != height:
            self._occ = np.zeros((STATIC_GRID_H, STATIC_GRID_W), np.float32)
            self._w, self._h, self._passes = width, height, 0
        self._passes += 1
        hit = np.zeros((STATIC_GRID_H, STATIC_GRID_W), bool)
        for box in boxes:
            r, c = self._center_cell(box.bbox)
            hit[r, c] = True
        self._occ = self._occ * STATIC_DECAY + hit * (1 - STATIC_DECAY)

    def is_settled(self, bbox) -> bool:
        """Место «насиженное»: центр силуэта почти всегда тут. Берём пик по окрестности 3x3,
        чтобы поймать точку, куда центр картины попадает чаще всего (он слегка гуляет по рамке)."""
        if self._occ is None or self._passes < STATIC_WARMUP:
            return False
        r, c = self._center_cell(bbox)
        peak = self._occ[max(0, r - 1):r + 2, max(0, c - 1):c + 2].max()
        return float(peak) >= STATIC_OCC_MIN


class PeopleWatcher:
    def __init__(self, camera_id: int, group_id: int):
        self.camera_id, self.group_id = camera_id, group_id
        self.enabled = False
        self.events_created = 0
        self.people_now = 0  # сколько людей в кадре на последнем проходе детектора
        self._tracker = PeopleTracker()
        self._static = StaticScene()  # неподвижную обстановку помним и между переподключениями
        self._last_detect = -1e9
        self._enabled_checked = -1e9

    def new_session(self) -> None:
        self._tracker = PeopleTracker()
        self._last_detect = -1e9
        self._enabled_checked = -1e9

    def _refresh_enabled(self, seconds: float) -> None:
        if seconds - self._enabled_checked < 5:  # флаг группы перечитываем раз в 5 секунд
            return
        self._enabled_checked = seconds
        with SessionLocal() as db:
            group = db.get(models.CameraGroup, self.group_id)
            self.enabled = bool(group and group.detect_people)
            if not self.enabled:
                self.people_now = 0

    def on_frame(self, frame: np.ndarray, seconds: float, when: datetime) -> None:
        self._refresh_enabled(seconds)
        if not self.enabled or seconds - self._last_detect < DETECT_INTERVAL:
            return
        analyzer = get_people()
        if analyzer is None:
            return
        self._last_detect = seconds
        try:
            boxes = analyzer.detect(frame)
            self.people_now = len(boxes)
            h, w = frame.shape[:2]
            self._static.observe(boxes, w, h)
            self._register(analyzer, self._tracker.update(frame, boxes, seconds), when - timedelta(seconds=seconds))
        except Exception:
            log.exception("Сбой детекции людей на камере %s", self.camera_id)

    def finish(self, when_base: datetime) -> None:
        analyzer = get_people()
        if analyzer is not None:
            self._register(analyzer, self._tracker.flush(), when_base)

    def _register(self, analyzer, tracks: list[_Track], when_base: datetime) -> None:
        for track in tracks:
            if track.frames < MIN_TRACK_FRAMES or track.best_frame is None:
                continue
            if self._static.is_settled(track.best_bbox):
                continue  # «насиженное» место — там всё время висит/стоит обстановка (картина, статуя, манекен)
            if not track.moved_enough:
                continue  # отсев неподвижных силуэтов, пока карта «насиженных» мест ещё набирается
            feats = analyzer.attributes(track.best_frame, track.best_bbox)
            height, width = track.best_frame.shape[:2]
            gender = feats.get("gender", {})
            event = models.PersonEvent(
                created_date=when_base + timedelta(seconds=track.last_time),
                camera_group_id=self.group_id, camera_id=self.camera_id,
                track_duration=round(track.duration, 3), track_frames=track.frames,
                bbox=[int(v) for v in track.best_bbox], frame_width=width, frame_height=height,
                detection_score=round(track.best_score, 4),
                gender=gender.get("name"), gender_confidence=gender.get("confidence"),
                attrs=feats.get("attrs", {}), upper_color=feats.get("upper_color"), lower_color=feats.get("lower_color"),
            )
            with SessionLocal() as db:
                db.add(event)
                db.flush()
                storage.delete_person_image(event.id)
                left, top, right, bottom = track.best_bbox
                crop = track.best_frame[top:bottom, left:right]
                storage.save_person_image(event.id, eng.encode_jpeg(_fit(crop, 360), 85))
                db.commit()
                self.events_created += 1
                broadcaster.publish("person_event_created", _event_dict(event))


def _fit(image: np.ndarray, max_side: int) -> np.ndarray:
    import cv2

    scale = max_side / max(image.shape[:2])
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image


def _event_dict(event: models.PersonEvent) -> dict:
    return {
        "id": event.id, "created_date": event.created_date.isoformat(),
        "camera_group_id": event.camera_group_id, "camera_id": event.camera_id,
        "track_duration": event.track_duration, "track_frames": event.track_frames,
        "bbox": event.bbox, "frame_width": event.frame_width, "frame_height": event.frame_height,
        "detection_score": event.detection_score, "gender": event.gender,
        "gender_confidence": event.gender_confidence, "attrs": event.attrs or {},
        "upper_color": event.upper_color, "lower_color": event.lower_color,
    }


def delete_person_events(db, *conditions) -> int:
    """Удаляет события людей по условию вместе со снимками."""
    ids = list(db.scalars(select(models.PersonEvent.id).where(*conditions)))
    for start in range(0, len(ids), 500):
        db.execute(delete(models.PersonEvent).where(models.PersonEvent.id.in_(ids[start:start + 500])))
    for event_id in ids:
        storage.delete_person_image(event_id)
    return len(ids)

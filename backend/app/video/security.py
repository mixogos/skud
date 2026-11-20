"""Охранная видеоаналитика камеры: вторжение в зону, праздношатание, скопление людей,
саботаж камеры.

Работает внутри потока камеры параллельно с распознаванием лиц и аналитикой людей.
Правила читаются из базы при подключении и после каждой правки. На каждое срабатывание
создаётся охранное событие (SecurityEvent) с кадром места и рассылается оповещение.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np
from sqlalchemy import delete, select

from .. import models, storage, webhooks
from ..db import SessionLocal
from ..notifications import broadcaster
from ..recognition import engine as eng
from ..recognition.people import PersonBox, get_people
from .people_stream import PeopleTracker

log = logging.getLogger("skud.security")

DETECT_INTERVAL = 0.4   # как часто искать людей для охранных правил, секунд
TAMPER_INTERVAL = 0.5   # как часто проверять саботаж
RULE_COOLDOWN = 15.0    # секунд: одно и то же правило не тревожит чаще
EVENT_IMAGE_SIDE = 640
PERSON_TYPES = {"intrusion", "loitering", "crowd"}

SEVERITY = {"intrusion": "alarm", "tampering": "alarm", "loitering": "warning", "crowd": "warning"}
TITLE = {
    "intrusion": "Вторжение в зону",
    "loitering": "Праздношатание",
    "crowd": "Скопление людей",
    "tampering": "Саботаж камеры",
}


def point_in_polygon(polygon, point) -> bool:
    """Лежит ли точка (x, y в долях кадра) внутри многоугольника. Пустой — весь кадр."""
    if not polygon or len(polygon) < 3:
        return True
    x, y = point
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def _footpoint(bbox, width: int, height: int) -> tuple[float, float]:
    """Точка «под ногами» силуэта в долях кадра — по ней судим о попадании в зону."""
    return ((bbox[0] + bbox[2]) / 2 / width, bbox[3] / height)


class TamperDetector:
    """Саботаж камеры: перекрытие обзора, расфокусировка, резкое смещение камеры.

    Копит эталон (фон и резкость) в спокойном состоянии и сравнивает с ним текущий кадр.
    Чтобы не ложно тревожить на проходящих людях и рывках, условие должно держаться HOLD секунд.
    """

    BW = 320
    HOLD = 3.0           # секунд условие держится, прежде чем счесть саботажем
    DARK = 22.0          # средняя яркость ниже — обзор перекрыт
    FLAT = 7.0           # разброс яркости ниже — кадр залит однотонным (накрыли)
    DEFOCUS_RATIO = 0.4  # резкость упала ниже этой доли эталона — расфокус
    MOVED = 55.0         # средняя разница с эталоном выше — камеру увели/сместили

    def __init__(self):
        self._bg: np.ndarray | None = None
        self._sharp_base = 0.0
        self._warm = 0
        self._since: float | None = None   # с какого момента держится аномалия
        self._reason = ""

    def _gray(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        return cv2.cvtColor(cv2.resize(frame, (self.BW, max(1, int(self.BW * h / w)))), cv2.COLOR_BGR2GRAY)

    def check(self, frame: np.ndarray, seconds: float, sensitivity: float) -> str | None:
        """Возвращает причину саботажа один раз при срабатывании, иначе None."""
        gray = self._gray(frame).astype(np.float32)
        sharp = float(cv2.Laplacian(gray, cv2.CV_32F).var())
        mean, std = float(gray.mean()), float(gray.std())
        k = sensitivity / 50.0  # 1.0 при чувствительности 50; выше — строже пороги
        reason = ""
        if self._bg is not None and self._warm >= 10:
            diff = float(np.abs(gray - self._bg).mean())
            if mean < self.DARK * k or std < self.FLAT * k:
                reason = "перекрыт обзор"
            elif self._sharp_base > 20 and sharp < self.DEFOCUS_RATIO * self._sharp_base:
                reason = "расфокусировка"
            elif diff > self.MOVED / max(k, 0.3):
                reason = "камера смещена"
        # Эталон обновляем только в спокойном состоянии, медленно.
        if self._bg is None:
            self._bg, self._sharp_base = gray.copy(), sharp
        elif not reason:
            self._bg = self._bg * 0.98 + gray * 0.02
            self._sharp_base = self._sharp_base * 0.95 + sharp * 0.05
            self._warm += 1
        if not reason:
            self._since, self._reason = None, ""
            return None
        if self._since is None or reason != self._reason:
            self._since, self._reason = seconds, reason
            return None
        if seconds - self._since >= self.HOLD:
            self._since = seconds + RULE_COOLDOWN  # не частить, пока держится
            return reason
        return None

    def new_session(self) -> None:
        self._since, self._reason = None, ""


@dataclass
class _Rule:
    id: int
    name: str
    type: str
    zone: list
    params: dict


@dataclass
class SecurityWatcher:
    camera_id: int
    dirty: bool = True
    rules: list[_Rule] = field(default_factory=list)
    _tracker: PeopleTracker = field(default_factory=PeopleTracker)
    _tamper: TamperDetector = field(default_factory=TamperDetector)
    _last_detect: float = -1e9
    _last_tamper: float = -1e9
    # (rule_id, track_id) → был ли силуэт в зоне на прошлом проходе / уже тревожили
    _inside: dict = field(default_factory=dict)
    _loiter_since: dict = field(default_factory=dict)
    _alarmed: dict = field(default_factory=dict)
    _crowd_since: dict = field(default_factory=dict)
    _cooldown: dict = field(default_factory=dict)  # rule_id → время последней тревоги

    def new_session(self) -> None:
        self._tracker = PeopleTracker()
        self._tamper.new_session()
        self._last_detect = self._last_tamper = -1e9
        self._inside.clear()
        self._loiter_since.clear()
        self._alarmed.clear()
        self._crowd_since.clear()

    def _reload(self) -> None:
        self.dirty = False
        with SessionLocal() as db:
            rows = db.scalars(
                select(models.SecurityRule).where(
                    models.SecurityRule.camera_id == self.camera_id, models.SecurityRule.active
                )
            ).all()
            self.rules = [_Rule(r.id, r.name, r.rule_type, list(r.zone or []), dict(r.params or {})) for r in rows]

    @property
    def _person_rules(self) -> list[_Rule]:
        return [r for r in self.rules if r.type in PERSON_TYPES]

    def on_frame(self, frame: np.ndarray, seconds: float, when: datetime) -> None:
        if self.dirty:
            self._reload()
        if not self.rules:
            return
        try:
            tamper_rules = [r for r in self.rules if r.type == "tampering"]
            if tamper_rules and seconds - self._last_tamper >= TAMPER_INTERVAL:
                self._last_tamper = seconds
                self._check_tamper(tamper_rules, frame, seconds, when)
            if self._person_rules and seconds - self._last_detect >= DETECT_INTERVAL:
                analyzer = get_people()
                if analyzer is not None:
                    self._last_detect = seconds
                    boxes = analyzer.detect(frame)
                    tracks = self._tracker.update(frame, boxes, seconds)  # завершённые нам не нужны
                    self._check_persons(frame, seconds, when)
        except Exception:
            log.exception("Сбой охранной аналитики камеры %s", self.camera_id)

    # --- Саботаж ---
    def _check_tamper(self, rules: list[_Rule], frame: np.ndarray, seconds: float, when: datetime) -> None:
        sensitivity = float(rules[0].params.get("sensitivity", 50))
        reason = self._tamper.check(frame, seconds, sensitivity)
        if not reason:
            return
        for rule in rules:
            if self._on_cooldown(rule.id, seconds):
                continue
            self._cooldown[rule.id] = seconds
            self._raise(rule, f"{rule.name}: {reason}", frame, [], when)

    # --- Люди в зонах ---
    def _check_persons(self, frame: np.ndarray, seconds: float, when: datetime) -> None:
        height, width = frame.shape[:2]
        live = [t for t in self._tracker._tracks if t.last_time == seconds]
        for rule in self._person_rules:
            in_zone = [t for t in live if point_in_polygon(rule.zone, _footpoint(t.bbox, width, height))]
            if rule.type == "intrusion":
                self._intrusion(rule, in_zone, live, frame, seconds, when, width, height)
            elif rule.type == "loitering":
                self._loitering(rule, in_zone, live, frame, seconds, when, width, height)
            elif rule.type == "crowd":
                self._crowd(rule, in_zone, frame, seconds, when, width, height)

    def _intrusion(self, rule, in_zone, live, frame, seconds, when, width, height) -> None:
        inside_ids = {t.id for t in in_zone}
        for track in in_zone:
            key = (rule.id, track.id)
            if not self._inside.get(key) and not self._alarmed.get(key):  # вошёл в зону впервые
                self._alarmed[key] = True
                if not self._on_cooldown(rule.id, seconds):
                    self._cooldown[rule.id] = seconds
                    self._raise(rule, f"{rule.name}: вход в запретную зону", frame,
                                self._norm_boxes([track.bbox], width, height), when)
        for track in live:
            key = (rule.id, track.id)
            self._inside[key] = track.id in inside_ids
            if track.id not in inside_ids:
                self._alarmed.pop(key, None)  # вышел — в следующий раз снова тревога

    def _loitering(self, rule, in_zone, live, frame, seconds, when, width, height) -> None:
        dwell = float(rule.params.get("dwell_seconds", 10))
        inside_ids = {t.id for t in in_zone}
        for track in in_zone:
            key = (rule.id, track.id)
            self._loiter_since.setdefault(key, seconds)
            if seconds - self._loiter_since[key] >= dwell and not self._alarmed.get(key):
                self._alarmed[key] = True
                if not self._on_cooldown(rule.id, seconds):
                    self._cooldown[rule.id] = seconds
                    self._raise(rule, f"{rule.name}: задержка в зоне дольше {int(dwell)} с", frame,
                                self._norm_boxes([track.bbox], width, height), when)
        for track in live:
            if track.id not in inside_ids:  # вышел из зоны — счётчик и тревога сбрасываются
                self._loiter_since.pop((rule.id, track.id), None)
                self._alarmed.pop((rule.id, track.id), None)

    def _crowd(self, rule, in_zone, frame, seconds, when, width, height) -> None:
        min_count = int(rule.params.get("min_count", 3))
        hold = float(rule.params.get("hold_seconds", 3))
        if len(in_zone) >= min_count:
            self._crowd_since.setdefault(rule.id, seconds)
            if seconds - self._crowd_since[rule.id] >= hold and not self._on_cooldown(rule.id, seconds):
                self._cooldown[rule.id] = seconds
                self._raise(rule, f"{rule.name}: в зоне {len(in_zone)} чел. (порог {min_count})", frame,
                            self._norm_boxes([t.bbox for t in in_zone], width, height), when)
        else:
            self._crowd_since.pop(rule.id, None)

    # --- Общее ---
    def _on_cooldown(self, rule_id: int, seconds: float) -> bool:
        return seconds - self._cooldown.get(rule_id, -1e9) < RULE_COOLDOWN

    @staticmethod
    def _norm_boxes(boxes, width: int, height: int) -> list:
        return [[round(b[0] / width, 4), round(b[1] / height, 4), round(b[2] / width, 4), round(b[3] / height, 4)] for b in boxes]

    def _raise(self, rule: _Rule, message: str, frame: np.ndarray, boxes: list, when: datetime) -> None:
        height, width = frame.shape[:2]
        event = models.SecurityEvent(
            created_date=when, camera_id=self.camera_id, rule_id=rule.id, rule_type=rule.type,
            rule_name=rule.name, severity=SEVERITY.get(rule.type, "alarm"), message=message,
            zone=rule.zone, boxes=boxes, frame_width=width, frame_height=height, has_image=True,
        )
        with SessionLocal() as db:
            if db.get(models.SecurityRule, rule.id) is None:  # правило удалили — перечитать
                self.dirty = True
                return
            db.add(event)
            db.flush()
            storage.save_security_image(event.id, eng.encode_jpeg(_fit(frame, EVENT_IMAGE_SIDE), 80))
            db.commit()
            payload = _event_dict(event)
            log.info("Охранная тревога [%s] камера %s: %s", rule.type, self.camera_id, message)
            broadcaster.publish("security_alarm", payload)
            webhooks.dispatch_security(db, payload)


def _fit(image: np.ndarray, max_side: int) -> np.ndarray:
    scale = max_side / max(image.shape[:2])
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image


def _event_dict(event: models.SecurityEvent) -> dict:
    return {
        "id": event.id, "created_date": event.created_date.isoformat(), "camera_id": event.camera_id,
        "rule_id": event.rule_id, "rule_type": event.rule_type, "rule_name": event.rule_name,
        "severity": event.severity, "message": event.message, "zone": event.zone or [], "boxes": event.boxes or [],
        "frame_width": event.frame_width, "frame_height": event.frame_height, "has_image": event.has_image,
        "acknowledged": event.acknowledged,
    }


def delete_security_events(db, *conditions) -> int:
    """Удаляет охранные события по условию вместе со снимками."""
    ids = list(db.scalars(select(models.SecurityEvent.id).where(*conditions)))
    for start in range(0, len(ids), 500):
        db.execute(delete(models.SecurityEvent).where(models.SecurityEvent.id.in_(ids[start:start + 500])))
    for event_id in ids:
        storage.delete_security_image(event_id)
    return len(ids)

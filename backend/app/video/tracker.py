"""Трекер лиц: связывает обнаружения на соседних кадрах в треки и хранит лучший кадр трека."""
from dataclasses import dataclass, field

import numpy as np
from pydantic import BaseModel, Field

from ..recognition.engine import Face, confidence


class StreamSettings(BaseModel):
    """Параметры обработки видео (аналог параметров видеопотока в FindFace Multi)."""

    process_fps: float = Field(default=12.0, gt=0, le=60, description="Сколько кадров в секунду анализировать")
    min_quality: float = Field(default=0.4, ge=0, le=1, description="Минимальное качество лучшего кадра для события")
    min_track_frames: int = Field(default=2, ge=1, description="Минимум кадров в треке (отсекает ложные срабатывания)")
    max_track_gap: float = Field(default=1.0, gt=0, description="Через сколько секунд без лица трек завершается")
    max_track_duration: float = Field(
        default=0, ge=0, description="Принудительно завершать трек через столько секунд (0 — не ограничивать)"
    )


@dataclass
class Track:
    id: int
    first_time: float
    last_time: float
    bbox: tuple[int, int, int, int]
    frames: int = 1
    best_quality: float = -1.0
    best_face: Face | None = None
    best_frame: np.ndarray | None = field(default=None, repr=False)
    best_time: float = 0.0
    prev_bbox: tuple[int, int, int, int] | None = None  # рамка на предыдущем кадре (для пересечения линий)
    successor_id: int | None = None  # трек-продолжение, если этот разрезан по времени

    def offer(self, frame: np.ndarray, face: Face, quality: float, time: float) -> None:
        if quality > self.best_quality:
            self.best_quality, self.best_face, self.best_time = quality, face, time
            self.best_frame = frame  # кадр не изменяется после чтения, копия не нужна

    @property
    def duration(self) -> float:
        return self.last_time - self.first_time


def _iou(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def _center_distance(a, b) -> float:
    """Расстояние между центрами в долях размера большей рамки."""
    ax, ay, bx, by = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    size = max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1], 1)
    return float(np.hypot(ax - bx, ay - by)) / size


class FaceTracker:
    IOU_MIN = 0.2
    DISTANCE_MAX = 0.8

    def __init__(self, settings: StreamSettings, hold_seconds: float = 0.0, hold_threshold: float = 0.0):
        self.settings = settings
        # Удержание человека: когда лицо на время пропадает (отвернулся, заслонили),
        # трек не завершается сразу, а hold_seconds секунд ждёт возвращения того же лица
        # (сверка по вектору признаков — «математическая модель» лица). Это не даёт
        # создать второе событие на одного и того же человека. Работает только для
        # трекинга лиц — когда в update передан движок; для трекинга тел выключено.
        self.hold_seconds = max(0.0, hold_seconds)
        self.hold_threshold = hold_threshold
        self._tracks: list[Track] = []
        self._held: list[Track] = []  # удерживаемые треки: лицо пропало, но человек ещё «ведётся»
        self._next_id = 1

    def update(
        self, frame: np.ndarray, faces: list[tuple[Face, float]], time: float, engine=None
    ) -> list[Track]:
        """Принимает лица очередного кадра (с оценками качества). Возвращает завершившиеся треки.

        Если передан `engine` и включено удержание, пропавшие на время лица не создают
        новых треков: при возвращении того же человека продолжается прежний трек.
        """
        holding = engine is not None and self.hold_seconds > 0 and self.hold_threshold > 0
        free_tracks = list(range(len(self._tracks)))
        free_faces = list(range(len(faces)))

        def assign(score_fn, accept):
            pairs = sorted(
                ((score_fn(self._tracks[t].bbox, faces[f][0].bbox), t, f) for t in free_tracks for f in free_faces),
                reverse=True,
            )
            for score, t, f in pairs:
                if accept(score) and t in free_tracks and f in free_faces:
                    track, (face, quality) = self._tracks[t], faces[f]
                    track.prev_bbox, track.bbox, track.last_time = track.bbox, face.bbox, time
                    track.frames += 1
                    track.offer(frame, face, quality, time)
                    free_tracks.remove(t)
                    free_faces.remove(f)

        # Сначала по пересечению рамок, затем — для быстрых движений — по близости центров.
        assign(_iou, lambda s: s >= self.IOU_MIN)
        assign(lambda a, b: -_center_distance(a, b), lambda s: -s <= self.DISTANCE_MAX)

        # Удержание: оставшиеся лица пробуем узнать среди удерживаемых — по вектору признаков.
        if holding and self._held and free_faces:
            self._revive(frame, faces, free_faces, time, engine)

        for f in free_faces:
            face, quality = faces[f]
            track = Track(self._next_id, time, time, face.bbox)
            track.offer(frame, face, quality, time)
            self._next_id += 1
            self._tracks.append(track)

        finished, alive = [], []
        limit = self.settings.max_track_duration
        for track in self._tracks:
            lost = time - track.last_time > self.settings.max_track_gap
            too_long = limit > 0 and track.duration >= limit
            if too_long:
                finished.append(track)
                if not lost:
                    # Человек ещё в кадре: его ведёт трек-продолжение, чтобы не потерять траекторию.
                    follower = Track(self._next_id, time, track.last_time, track.bbox, frames=0)
                    track.successor_id = follower.id
                    self._next_id += 1
                    alive.append(follower)
            elif lost:
                # Лицо пропало: удерживаем человека (ждём возвращения), иначе завершаем сразу.
                (self._held if holding else finished).append(track)
            else:
                alive.append(track)
        self._tracks = alive
        # Удерживаемые, не вернувшиеся за hold_seconds, — завершаем.
        if self._held:
            kept = []
            for track in self._held:
                (finished if time - track.last_time > self.hold_seconds else kept).append(track)
            self._held = kept
        return finished

    def _revive(self, frame, faces, free_faces: list[int], time: float, engine) -> None:
        """Возвращает к жизни удерживаемый трек, если среди новых лиц есть тот же человек."""
        fresh = [faces[f][0] for f in free_faces if faces[f][0].embedding is None]
        if fresh:
            engine.extract(frame, fresh)
        for track in self._held:
            bf = track.best_face
            if bf is not None and bf.embedding is None and track.best_frame is not None:
                engine.extract(track.best_frame, [bf])
        pairs = []
        for hi, track in enumerate(self._held):
            bf = track.best_face
            if bf is None or bf.embedding is None:
                continue
            hv = np.asarray(bf.embedding, dtype=np.float32)
            for f in free_faces:
                fv = faces[f][0].embedding
                if fv is None:
                    continue
                sim = confidence(hv, np.asarray(fv, dtype=np.float32))
                if sim >= self.hold_threshold:
                    pairs.append((sim, hi, f))
        if not pairs:
            return
        pairs.sort(key=lambda p: p[0], reverse=True)
        used_h, used_f = set(), set()
        for _sim, hi, f in pairs:
            if hi in used_h or f in used_f:
                continue
            track, (face, quality) = self._held[hi], faces[f]
            track.prev_bbox, track.bbox, track.last_time = track.bbox, face.bbox, time
            track.frames += 1
            track.offer(frame, face, quality, time)
            self._tracks.append(track)
            used_h.add(hi)
            used_f.add(f)
        self._held = [t for i, t in enumerate(self._held) if i not in used_h]
        for f in sorted(used_f, reverse=True):
            free_faces.remove(f)

    @property
    def tracks(self) -> list[Track]:
        """Живые треки."""
        return self._tracks

    def flush(self) -> list[Track]:
        """Завершает все оставшиеся треки (конец видео)."""
        finished, self._tracks, self._held = self._tracks + self._held, [], []
        return finished

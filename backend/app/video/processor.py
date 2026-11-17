"""Обработка видео (аналог findface-video-worker): чтение кадров, обнаружение лиц,
трекинг, выбор лучшего кадра и извлечение вектора признаков для каждого трека."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import cv2
import numpy as np

from ..recognition.engine import Face, FaceEngine
from .quality import face_quality
from .tracker import FaceTracker, StreamSettings, Track


class VideoError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    fps: float
    frame_count: int
    width: int
    height: int

    @property
    def duration(self) -> float:
        return self.frame_count / self.fps if self.fps > 0 else 0.0


@dataclass
class TrackResult:
    """Итог одного трека: лучший кадр человека, прошедшего перед камерой."""

    track_id: int
    face: Face  # с заполненным embedding
    frame: np.ndarray
    quality: float
    time: float  # секунд от начала видео, момент лучшего кадра
    first_time: float
    duration: float
    frames: int
    liveness: float | None = None  # заполняется при создании события
    features: dict | None = None  # атрибуты лица, заполняются при создании события


def open_video(path: Path) -> tuple[cv2.VideoCapture, VideoInfo]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise VideoError("Не удалось открыть видеофайл: формат не поддерживается или файл повреждён")
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps <= 0 or fps > 240:
        fps = 25.0
    info = VideoInfo(
        fps=float(fps),
        frame_count=max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT))),
        width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    )
    if info.width <= 0 or info.height <= 0:
        capture.release()
        raise VideoError("В файле нет видеодорожки")
    return capture, info


def probe_video(path: Path) -> VideoInfo:
    capture, info = open_video(path)
    try:
        ok, _ = capture.read()
        if not ok:
            raise VideoError("Не удалось прочитать ни одного кадра")
    finally:
        capture.release()
    return info


def analyze_video(
    path: Path,
    engine: FaceEngine,
    settings: StreamSettings,
    on_progress: Callable[[float, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[TrackResult]:
    """Проходит по видео и выдаёт результат каждого завершившегося трека."""
    capture, info = open_video(path)
    tracker = FaceTracker(settings)
    step = max(1, round(info.fps / settings.process_fps))
    index = processed = 0

    def finish(tracks: list[Track]) -> Iterator[TrackResult]:
        for track in tracks:
            if track.frames < settings.min_track_frames or track.best_quality < settings.min_quality:
                continue
            engine.extract(track.best_frame, [track.best_face])
            yield TrackResult(
                track_id=track.id,
                face=track.best_face,
                frame=track.best_frame,
                quality=track.best_quality,
                time=track.best_time,
                first_time=track.first_time,
                duration=track.duration,
                frames=track.frames,
            )

    try:
        while True:
            if index % step:
                if not capture.grab():  # пропускаемый кадр: без декодирования в массив
                    break
                index += 1
                continue
            ok, frame = capture.read()
            if not ok:
                break
            time = index / info.fps
            faces = [(face, face_quality(frame, face)) for face in engine.detect(frame)]
            yield from finish(tracker.update(frame, faces, time))
            index += 1
            processed += 1
            if on_progress:
                on_progress(min(1.0, index / info.frame_count) if info.frame_count else 0.0, processed)
            if should_stop and should_stop():
                return
        yield from finish(tracker.flush())
    finally:
        capture.release()

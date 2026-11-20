"""Видеопотоки с камер: сервер сам подключается к камере, читает кадры и создаёт события.

У каждой включённой камеры свой поток выполнения. Отдельный поток-читатель держит
только самый свежий кадр, поэтому распознавание никогда не отстаёт от реального времени:
кадры, которые не успели обработать, пропускаются. При обрыве связи камера
переподключается сама.
"""
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# RTSP по TCP: по UDP кадры на загруженной сети приходят битыми. Задаётся до первого подключения.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
import numpy as np
from sqlalchemy import select

from .. import access, config, events, matching, models
from ..db import SessionLocal
from ..recognition import engine as eng
from ..settings_store import get_settings
from .analytics import CameraAnalytics
from .people_stream import PeopleWatcher
from .security import SecurityWatcher
from .processor import TrackResult
from .quality import face_quality
from .tracker import FaceTracker, StreamSettings, Track

log = logging.getLogger("skud.streams")

URL_SCHEMES = ("rtsp", "rtsps", "http", "https", "rtmp")
OPEN_TIMEOUT_MS = 10_000
READ_TIMEOUT = 10.0  # секунд без кадров — поток считается оборванным
RETRY_DELAYS = (2, 5, 10, 30)  # паузы между попытками подключения, секунд
MASK = "***"


def is_webcam(url: str) -> bool:
    return bool(re.fullmatch(r"\d", url))


def is_network(url: str) -> bool:
    return urlsplit(url).scheme.lower() in URL_SCHEMES


def validate_url(url: str) -> str:
    """Проверяет адрес источника. Возвращает очищенный адрес или вызывает ValueError."""
    url = url.strip()
    if is_webcam(url):
        return url  # номер веб-камеры, подключённой к серверу
    if is_network(url):
        if not urlsplit(url).hostname:
            raise ValueError("В адресе потока не указан узел")
        return url
    if config.CAMERA_FILE_SOURCES and Path(url).is_file():
        return url  # видеофайл на сервере, проигрывается по кругу (для проверки и демонстрации)
    raise ValueError(
        "Адрес потока должен начинаться с rtsp://, rtsps://, http://, https:// или rtmp://; "
        "для веб-камеры сервера укажите её номер (0, 1, ...)"
    )


def mask_url(url: str) -> str:
    """Скрывает пароль в адресе: rtsp://user:***@host/..."""
    if not is_network(url):
        return url
    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{parts.username or ''}:{MASK}@{host}"))


def open_source(url: str) -> cv2.VideoCapture:
    if is_webcam(url):
        return cv2.VideoCapture(int(url))
    if is_network(url):
        return cv2.VideoCapture(
            url, cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(READ_TIMEOUT * 1000)],
        )
    return cv2.VideoCapture(url)


class _Reader(threading.Thread):
    """Читает кадры без остановки и хранит только последний."""

    def __init__(self, capture: cv2.VideoCapture, pace: float):
        super().__init__(name="camera-reader", daemon=True)
        self.capture, self.pace = capture, pace
        self.cond = threading.Condition()
        self.frame: np.ndarray | None = None
        self.seq = 0
        self.ended = False
        self.stop_flag = False

    def run(self) -> None:
        try:
            while not self.stop_flag:
                ok, frame = self.capture.read()
                if not ok:
                    break
                with self.cond:
                    self.frame, self.seq = frame, self.seq + 1
                    self.cond.notify_all()
                if self.pace:
                    time.sleep(self.pace)
        except Exception:
            log.exception("Сбой чтения кадров")
        finally:
            with self.cond:
                self.ended = True
                self.cond.notify_all()
            self.capture.release()

    def next(self, last_seq: int, timeout: float) -> tuple[np.ndarray | None, int]:
        """Ждёт кадр новее last_seq. Возвращает (None, seq), если поток кончился или молчит."""
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or self.ended, timeout)
            if self.seq == last_seq:
                return None, last_seq
            return self.frame, self.seq


class CameraStream:
    def __init__(self, camera_id: int, url: str, group_id: int, settings: StreamSettings):
        self.camera_id, self.url, self.group_id, self.settings = camera_id, url, group_id, settings
        self.state, self.message = "CONNECTING", ""
        self.frames_processed = 0
        self.events_created = 0
        self.last_frame_date: datetime | None = None
        self.width = self.height = 0
        self._last_frame: np.ndarray | None = None
        self._overlay: list[tuple[tuple[int, int, int, int], bool]] = []  # (рамка лица, тревога) для онлайн-видео
        self._alerts: dict[int, bool] = {}  # track_id → показывать красным (список наблюдения / отказ в проходе)
        self.analytics = CameraAnalytics(camera_id)
        self.people = PeopleWatcher(camera_id, group_id)
        self.security = SecurityWatcher(camera_id)
        self._times: deque[float] = deque(maxlen=50)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"camera-{camera_id}", daemon=True)

    # --- Управление ---
    def start(self) -> None:
        self._thread.start()

    def stop(self, wait: float = 3.0) -> None:
        self._stop.set()
        self._thread.join(timeout=wait)

    @property
    def fps(self) -> float:
        times = list(self._times)
        if len(times) < 2 or time.monotonic() - times[-1] > 5:
            return 0.0
        return round((len(times) - 1) / max(times[-1] - times[0], 1e-6), 1)

    def snapshot(self, max_side: int = 960, boxes: bool = False) -> bytes | None:
        frame = self._last_frame
        if frame is None:
            return None
        if boxes and self._overlay:
            frame = frame.copy()
            thick = max(2, round(frame.shape[1] / 480))
            for (left, top, right, bottom), alert in self._overlay:
                color = (40, 40, 230) if alert else (60, 200, 70)  # BGR: красный на тревоге, иначе зелёный
                cv2.rectangle(frame, (left, top), (right, bottom), color, thick)
        scale = max_side / max(frame.shape[:2])
        if scale < 1:
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        return eng.encode_jpeg(frame, 80)

    def _set_state(self, state: str, message: str = "") -> None:
        if self._stop.is_set() or (state, message) == (self.state, self.message):
            return
        self.state, self.message = state, message
        with SessionLocal() as db:
            camera = db.get(models.Camera, self.camera_id)
            if camera is not None:
                camera.state, camera.state_message = state, message[:1024]
                db.commit()

    # --- Работа ---
    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self._set_state("CONNECTING", self.message)
            try:
                engine = eng.get_engine()
                capture = open_source(self.url)
                if not capture.isOpened():
                    capture.release()
                    raise ConnectionError("Не удалось подключиться к камере: проверьте адрес, логин и пароль")
                got_frames = self._session(engine, capture)
                error = "Поток прервался, переподключение"
            except (ConnectionError, eng.ModelsMissing) as exc:
                got_frames, error = False, str(exc)
            except Exception as exc:
                log.exception("Сбой камеры %s", self.camera_id)
                got_frames, error = False, f"Внутренняя ошибка: {exc}"
            if self._stop.is_set():
                return
            attempt = 0 if got_frames else attempt + 1
            if got_frames and not is_webcam(self.url) and not is_network(self.url):
                continue  # видеофайл закончился — проигрываем заново
            self._set_state("ERROR", error)
            self._stop.wait(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1] if attempt else RETRY_DELAYS[0])

    def _session(self, engine: eng.FaceEngine, capture: cv2.VideoCapture) -> bool:
        """Одно подключение к камере: от первого кадра до обрыва. Возвращает, были ли кадры."""
        pace = 0.0
        if not is_webcam(self.url) and not is_network(self.url):
            fps = capture.get(cv2.CAP_PROP_FPS)
            pace = 1.0 / fps if fps and 0 < fps <= 240 else 0.04
        reader = _Reader(capture, pace)
        reader.start()
        with SessionLocal() as db:
            rt = get_settings(db)
        # Удержание человека: трек держится hold_seconds даже когда лицо на время пропало.
        tracker = FaceTracker(
            self.settings, hold_seconds=rt.dedup_window, hold_threshold=rt.face_confidence_threshold
        )
        self.analytics.new_session()
        self.people.new_session()
        self.security.new_session()
        interval = 1.0 / self.settings.process_fps
        started_wall, started = models.utcnow(), time.monotonic()
        seq, got = 0, False
        try:
            while not self._stop.is_set():
                frame, seq = reader.next(seq, READ_TIMEOUT)
                if frame is None:
                    break
                tick = time.monotonic()
                now = tick - started
                if not got:
                    got = True
                    self.height, self.width = frame.shape[:2]
                    self._set_state("ONLINE")
                faces = [(face, face_quality(frame, face)) for face in engine.detect(frame)]
                finished = tracker.update(frame, faces, now, engine)
                self._update_overlay(engine, tracker.tracks, now)
                self.analytics.on_frame(
                    frame, [face for face, _ in faces], tracker.tracks + finished, now, started_wall + timedelta(seconds=now)
                )
                self._register(engine, finished, started_wall)
                self.people.on_frame(frame, now, started_wall + timedelta(seconds=now))
                self.security.on_frame(frame, now, started_wall + timedelta(seconds=now))
                self._last_frame = frame
                self.frames_processed += 1
                self.last_frame_date = models.utcnow()
                self._times.append(time.monotonic())
                self._stop.wait(max(0.0, interval - (time.monotonic() - tick)))
            if got and not self._stop.is_set():
                self._register(engine, tracker.flush(), started_wall)
                self.people.finish(started_wall)
        finally:
            reader.stop_flag = True
        return got

    def _update_overlay(self, engine: eng.FaceEngine, live: list[Track], now: float) -> None:
        """Готовит рамки лиц для онлайн-видео: зелёные, красные — для списков наблюдения и отказа в проходе.

        Цвет определяется один раз на трек по лучшему кадру (сопоставление с картотекой), затем кэшируется.
        """
        try:
            for track in live:
                if track.id in self._alerts or track.best_face is None or track.best_quality < self.settings.min_quality:
                    continue
                if track.best_face.embedding is None:
                    engine.extract(track.best_frame, [track.best_face])
                vector = np.asarray(track.best_face.embedding, dtype=np.float32)
                with SessionLocal() as db:
                    group = db.get(models.CameraGroup, self.group_id)
                    settings = get_settings(db)
                    match = matching.find_match(db, vector, models.utcnow(), group, settings)
                    alert = match is not None and any(wl.access_policy == "deny" for wl in match.watch_lists)
                    if group is not None and group.access_control:
                        decision, _ = access.decide(match, models.utcnow())
                        alert = alert or decision == "denied"
                self._alerts[track.id] = alert
        except Exception:
            log.exception("Сбой разметки лиц в онлайн-видео камеры %s", self.camera_id)
        self._overlay = [(tuple(int(v) for v in t.bbox), self._alerts.get(t.id, False))
                         for t in live if t.last_time == now]
        alive_ids = {t.id for t in live}
        for track_id in [k for k in self._alerts if k not in alive_ids]:
            self._alerts.pop(track_id, None)

    def _register(self, engine: eng.FaceEngine, tracks: list[Track], started_wall: datetime) -> None:
        for track in tracks:
            if track.frames < self.settings.min_track_frames or track.best_quality < self.settings.min_quality:
                self.analytics.on_track_finished(track, track.successor_id, None, None)
                continue
            if track.best_face.embedding is None:
                engine.extract(track.best_frame, [track.best_face])
            result = TrackResult(
                track_id=track.id, face=track.best_face, frame=track.best_frame, quality=track.best_quality,
                time=track.best_time, first_time=track.first_time, duration=track.duration, frames=track.frames,
            )
            with SessionLocal() as db:
                reg = events.process_track(
                    db, result,
                    created_date=started_wall + timedelta(seconds=track.best_time),
                    camera_group_id=self.group_id, camera_id=self.camera_id,
                )
                db.commit()
                if reg.event is not None:
                    self.events_created += 1
                    events.publish_event(db, reg.event, created=True)
            self.analytics.on_track_finished(
                track, track.successor_id, reg.event.id if reg.event else None, reg.match.card.id if reg.match else None
            )


class StreamManager:
    """Запускает и останавливает потоки камер вслед за изменениями в базе."""

    def __init__(self):
        self._streams: dict[int, CameraStream] = {}
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        with SessionLocal() as db:
            cameras = db.scalars(select(models.Camera).where(models.Camera.external_detector.is_(False))).all()
            ids = [c.id for c in cameras]
        self._running = True
        for camera_id in ids:
            self.sync(camera_id)

    def stop(self) -> None:
        self._running = False
        with self._lock:
            streams, self._streams = list(self._streams.values()), {}
        for stream in streams:
            stream._stop.set()
        for stream in streams:
            stream.stop()

    def sync(self, camera_id: int) -> None:
        """Приводит поток камеры в соответствие с записью в базе (после создания, правки, удаления)."""
        with self._lock:
            old = self._streams.pop(camera_id, None)
            if old is not None:
                old.stop()
            with SessionLocal() as db:
                camera = db.get(models.Camera, camera_id)
                if camera is None or camera.external_detector:
                    return
                wanted = self._running and camera.active and bool(camera.url)
                camera.state = "CONNECTING" if wanted else "DISABLED"
                camera.state_message = ""
                db.commit()
                if not wanted:
                    return
                stream = CameraStream(
                    camera.id, camera.url, camera.group_id, StreamSettings(**(camera.stream_settings or {}))
                )
            self._streams[camera_id] = stream
            stream.start()

    def refresh_analytics(self, camera_id: int) -> None:
        """Счётчики или линии камеры изменились: поток перечитает их на следующем кадре."""
        stream = self._streams.get(camera_id)
        if stream is not None:
            stream.analytics.dirty = True

    def refresh_security(self, camera_id: int) -> None:
        """Правила охранной аналитики изменились: поток перечитает их на следующем кадре."""
        stream = self._streams.get(camera_id)
        if stream is not None:
            stream.security.dirty = True

    def get(self, camera_id: int) -> CameraStream | None:
        return self._streams.get(camera_id)


stream_manager = StreamManager()

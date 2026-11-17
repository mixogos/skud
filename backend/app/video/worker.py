"""Фоновая очередь обработки видеофайлов (аналог findface-video-manager).

Один поток обрабатывает файлы по очереди: распознавание на процессоре и так
занимает все ядра, параллельная обработка только замедлила бы каждый файл.
"""
import logging
import queue
import threading
import time
from datetime import timedelta

from sqlalchemy import select

from .. import episodes, events, models, storage
from ..db import SessionLocal
from ..recognition import engine as eng
from .processor import VideoError, analyze_video
from .tracker import StreamSettings

log = logging.getLogger("skud.video")

ACTIVE_STATES = ("QUEUED", "PROCESSING")


def process_video(video_id: int) -> None:
    """Обрабатывает один видеофайл от начала до конца и записывает события."""
    with SessionLocal() as db:
        video = db.get(models.VideoArchive, video_id)
        if video is None or video.state != "QUEUED":
            return
        if video.cancel_requested:
            video.state, video.cancel_requested = "CANCELLED", False
            db.commit()
            return
        # Результаты прошлой обработки этого файла заменяются новыми.
        events.delete_video_results(db, video.id)
        video.state, video.error = "PROCESSING", ""
        video.progress, video.frames_processed, video.events_created = 0.0, 0, 0
        video.started_date, video.finished_date = models.utcnow(), None
        db.commit()

        started = video.started_date
        group_id = video.camera_group_id
        settings = StreamSettings(**(video.stream_settings or {}))
        state = {"progress": 0.0, "frames": 0, "saved": time.monotonic(), "cancel": False}

        def save_progress(force: bool = False) -> None:
            # В базу пишем не чаще раза в секунду; заодно проверяем запрос на остановку.
            if not force and time.monotonic() - state["saved"] < 1.0:
                return
            state["saved"] = time.monotonic()
            video.progress, video.frames_processed = round(state["progress"], 4), state["frames"]
            db.commit()
            db.refresh(video, ["cancel_requested"])
            state["cancel"] = video.cancel_requested

        def on_progress(progress: float, frames: int) -> None:
            state["progress"], state["frames"] = progress, frames
            save_progress()

        try:
            engine = eng.get_engine()
            for result in analyze_video(
                storage.video_path(video.file_name), engine, settings, on_progress, lambda: state["cancel"]
            ):
                event = events.register_track(
                    db,
                    result,
                    created_date=started + timedelta(seconds=result.time),
                    camera_group_id=group_id,
                    video_archive_id=video.id,
                )
                if event is None:  # правила списков или дедупликация: событие не создаётся
                    continue
                video.events_created += 1
                db.commit()
                events.publish_event(db, event, created=True)
            save_progress(force=True)
            episodes.close_all(db, video.id)
            if state["cancel"]:
                video.state = "CANCELLED"
            else:
                video.state, video.progress = "COMPLETED", 1.0
        except (VideoError, eng.ModelsMissing) as exc:
            db.rollback()
            video.state, video.error = "FAILED", str(exc)[:1024]
        except Exception as exc:  # обработчик не должен падать из-за одного файла
            log.exception("Ошибка обработки видео %s", video_id)
            db.rollback()
            video.state, video.error = "FAILED", f"Внутренняя ошибка: {exc}"[:1024]
        video.cancel_requested = False
        video.finished_date = models.utcnow()
        db.commit()


class VideoWorker:
    def __init__(self):
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # После перезапуска сервера: прерванные и ожидавшие файлы обрабатываются заново.
        with SessionLocal() as db:
            pending = db.scalars(
                select(models.VideoArchive).where(models.VideoArchive.state.in_(ACTIVE_STATES)).order_by(models.VideoArchive.id)
            ).all()
            for video in pending:
                video.state = "QUEUED"
            db.commit()
            ids = [v.id for v in pending]
        self._queue = queue.Queue()
        for video_id in ids:
            self._queue.put(video_id)
        self._thread = threading.Thread(target=self._run, name="video-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=30)
        self._thread = None

    def enqueue(self, video_id: int) -> None:
        self._queue.put(video_id)

    def _run(self) -> None:
        while True:
            video_id = self._queue.get()
            if video_id is None:
                return
            try:
                process_video(video_id)
            except Exception:
                log.exception("Сбой обработчика видео на файле %s", video_id)


video_worker = VideoWorker()

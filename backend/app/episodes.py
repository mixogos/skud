"""Эпизоды: автоматическое объединение событий одного человека."""
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session as DbSession

from . import models
from .recognition.engine import confidence
from .settings_store import RuntimeSettings


def _scope(video_archive_id: int | None):
    """Эпизоды видеофайла живут на шкале времени этого файла и не смешиваются с другими."""
    E = models.HumanEpisode
    return E.video_archive_id.is_(None) if video_archive_id is None else E.video_archive_id == video_archive_id


def close_stale(db: DbSession, now: datetime, settings: RuntimeSettings, video_archive_id: int | None = None) -> None:
    """Закрывает LIVE-эпизоды, в которых давно нет событий или которые длятся слишком долго."""
    E = models.HumanEpisode
    db.execute(
        update(E)
        .where(
            E.open,
            _scope(video_archive_id),
            or_(
                E.last_event_date < now - timedelta(seconds=settings.episode_event_timeout),
                E.created_date < now - timedelta(seconds=settings.episode_max_duration),
            ),
        )
        .values(open=False, closed_date=now)
    )


def close_all(db: DbSession, video_archive_id: int) -> None:
    """Закрывает все эпизоды видеофайла (обработка файла завершена)."""
    E = models.HumanEpisode
    db.execute(
        update(E).where(E.open, E.video_archive_id == video_archive_id).values(open=False, closed_date=models.utcnow())
    )


def assign_episode(db: DbSession, event: models.FaceEvent, settings: RuntimeSettings, threshold: float) -> models.HumanEpisode:
    """Добавляет событие в подходящий LIVE-эпизод или открывает новый.

    Подходящий эпизод — с той же карточкой, а если совпадения с картотекой нет —
    с лицом, похожим на лучшее лицо эпизода не ниже порога.
    """
    E = models.HumanEpisode
    now = event.created_date
    close_stale(db, now, settings, event.video_archive_id)
    vector = np.frombuffer(event.embedding, dtype=np.float32)

    chosen, chosen_score = None, 0.0
    for episode in db.scalars(select(E).where(E.open, _scope(event.video_archive_id))):
        if event.matched_card_id and episode.matched_card_id:
            if episode.matched_card_id != event.matched_card_id:
                continue  # разные карточки — заведомо разные люди
            score = 2.0  # одна и та же карточка важнее любой схожести
        else:
            score = confidence(vector, np.frombuffer(episode.embedding, dtype=np.float32))
            if score < threshold:
                continue
        if score > chosen_score:
            chosen, chosen_score = episode, score

    if chosen is None:
        chosen = E(
            created_date=now,
            last_event_date=now,
            embedding=event.embedding,
            best_quality=-1.0,
            video_archive_id=event.video_archive_id,
            camera_groups=[],
            matched_lists=[],
        )
        db.add(chosen)
        db.flush()

    chosen.events_count += 1
    chosen.last_event_date = max(chosen.last_event_date, now)
    if event.quality > chosen.best_quality:
        chosen.best_quality, chosen.best_event_id, chosen.embedding = event.quality, event.id, event.embedding
    if event.matched and not chosen.matched:
        chosen.matched, chosen.matched_card_id = True, event.matched_card_id
        chosen.confidence = event.confidence
    elif event.matched and (event.confidence or 0) > (chosen.confidence or 0):
        chosen.confidence = event.confidence
    chosen.matched_lists = sorted(set(chosen.matched_lists or []) | set(event.matched_lists or []))
    if event.camera_group_id is not None:
        chosen.camera_groups = sorted(set(chosen.camera_groups or []) | {event.camera_group_id})
    if not event.acknowledged:
        chosen.acknowledged = False
    event.episode_id = chosen.id
    return chosen


def refresh_acknowledged(db: DbSession, episode_id: int) -> models.HumanEpisode | None:
    """Эпизод считается принятым, когда приняты все его события."""
    episode = db.get(models.HumanEpisode, episode_id)
    if episode is None:
        return None
    pending = db.scalar(
        select(models.FaceEvent.id)
        .where(models.FaceEvent.episode_id == episode_id, models.FaceEvent.acknowledged.is_(False))
        .limit(1)
    )
    episode.acknowledged = pending is None
    return episode

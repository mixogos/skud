"""События распознавания лиц: создание по итогам трека, представление, удаление."""
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from . import access, episodes, matching, models, storage, webhooks
from .notifications import broadcaster
from .recognition import engine as eng
from .recognition.attributes import get_attributes
from .recognition.liveness import get_liveness
from .settings_store import get_settings
from .video.processor import TrackResult


def _is_duplicate(
    db: DbSession,
    group: models.CameraGroup | None,
    match: matching.Match | None,
    vector: np.ndarray,
    when: datetime,
    video_archive_id: int | None,
    threshold: float,
) -> bool:
    """Дедупликация: тот же человек уже отмечен в этой группе камер за последние секунды."""
    if group is None or not group.deduplicate:
        return False
    E = models.FaceEvent
    conds = [
        E.camera_group_id == group.id,
        E.created_date >= when - timedelta(seconds=group.deduplicate_delay),
        E.created_date <= when,
        E.video_archive_id.is_(None) if video_archive_id is None else E.video_archive_id == video_archive_id,
    ]
    if match is not None:
        return db.scalar(select(E.id).where(*conds, E.matched_card_id == match.card.id).limit(1)) is not None
    for (blob,) in db.execute(select(E.embedding).where(*conds, E.matched.is_(False))):
        if eng.confidence(vector, np.frombuffer(blob, dtype=np.float32)) >= threshold:
            return True
    return False


@dataclass
class Registration:
    """Итог обработки трека. Решение о доступе есть всегда, даже если событие не создано."""

    event: models.FaceEvent | None
    match: matching.Match | None
    access: str | None  # granted | denied | None (группа без контроля доступа)
    access_reason: str
    skipped: str = ""  # почему событие не создано


def register_track(db: DbSession, result: TrackResult, **kwargs) -> models.FaceEvent | None:
    """То же, что process_track, но возвращает только событие (или None)."""
    return process_track(db, result, **kwargs).event


def process_track(
    db: DbSession,
    result: TrackResult,
    *,
    created_date: datetime,
    camera_group_id: int | None,
    camera_id: int | None = None,
    video_archive_id: int | None = None,
) -> Registration:
    """Превращает итог трека в событие: сопоставляет с картотекой, принимает решение
    о доступе, применяет правила списков наблюдения и дедупликации, сохраняет кадры,
    относит событие к эпизоду.

    Фиксация транзакции и вызов publish_event — на вызывающем.
    """
    settings = get_settings(db)
    group = db.get(models.CameraGroup, camera_group_id) if camera_group_id is not None else None
    face = result.face
    vector = np.asarray(face.embedding, dtype=np.float32)
    threshold = matching.generic_threshold(group, settings)

    match = matching.find_match(db, vector, created_date, group, settings)
    decision, reason = access.decide(match, created_date) if group is not None and group.access_control else (None, "")

    if result.liveness is None and (checker := get_liveness()) is not None:
        result.liveness = checker.score(result.frame, face.bbox)
    if decision is not None and group.liveness_check:
        # Закрытая по умолчанию логика: без оценки живости дверь не открывается.
        if result.liveness is None:
            decision, reason = "denied", "Проверка живости недоступна: модели не установлены"
        elif result.liveness < settings.liveness_threshold:
            decision, reason = "denied", "Подозрение на подделку: фото или экран вместо лица"

    def skip(why: str) -> Registration:
        return Registration(None, match, decision, reason, why)

    if match is not None:
        if all(wl.ignore_events for wl in match.watch_lists):
            return skip("Список наблюдения не создаёт события")
        needs_ack = any(wl.acknowledge for wl in match.watch_lists)
    else:
        if not settings.unmatched_events_enabled:
            return skip("События без совпадения отключены")
        needs_ack = settings.unmatched_acknowledge
    if _is_duplicate(db, group, match, vector, created_date, video_archive_id, threshold):
        return skip("Повторное событие (дедупликация)")
    if result.features is None:
        analyzer = get_attributes()
        result.features = analyzer.analyze(result.frame, face.bbox) if analyzer is not None else {}

    height, width = result.frame.shape[:2]
    event = models.FaceEvent(
        created_date=created_date,
        camera_group_id=camera_group_id,
        camera_id=camera_id,
        video_archive_id=video_archive_id,
        video_offset=round(result.time, 3) if video_archive_id is not None else None,
        track_duration=round(result.duration, 3),
        track_frames=result.frames,
        bbox=[int(v) for v in face.bbox],
        frame_width=width,
        frame_height=height,
        detection_score=round(face.detection_score, 4),
        quality=result.quality,
        liveness=result.liveness,
        features=result.features,
        embedding=vector.tobytes(),
        matched=match is not None,
        matched_card_id=match.card.id if match else None,
        matched_face_id=match.face_id if match else None,
        confidence=round(match.confidence, 4) if match else None,
        matched_lists=[wl.id for wl in match.watch_lists] if match else [],
        acknowledged=not needs_ack,
        access=decision,
        access_reason=reason,
    )
    db.add(event)
    db.flush()
    storage.delete_event_files(event.id)  # SQLite может повторно выдать id удалённой записи
    storage.save_event_file(event.id, "thumb", eng.make_thumbnail(result.frame, face.bbox))
    storage.save_event_file(event.id, "full", eng.encode_jpeg(result.frame, 85))
    episodes.assign_episode(db, event, settings, threshold)
    return Registration(event, match, decision, reason)


# --- Представление ---
def event_dict(event: models.FaceEvent, card_names: dict[int, str]) -> dict:
    return {
        "id": event.id,
        "created_date": event.created_date,
        "camera_group_id": event.camera_group_id,
        "camera_id": event.camera_id,
        "video_archive_id": event.video_archive_id,
        "video_offset": event.video_offset,
        "track_duration": event.track_duration,
        "track_frames": event.track_frames,
        "bbox": event.bbox,
        "frame_width": event.frame_width,
        "frame_height": event.frame_height,
        "detection_score": event.detection_score,
        "quality": event.quality,
        "liveness": event.liveness,
        "features": event.features or {},
        "matched": event.matched,
        "matched_card_id": event.matched_card_id,
        "matched_card_name": card_names.get(event.matched_card_id) if event.matched_card_id else None,
        "matched_face_id": event.matched_face_id,
        "confidence": event.confidence,
        "matched_lists": event.matched_lists or [],
        "acknowledged": event.acknowledged,
        "acknowledged_by": event.acknowledged_by,
        "acknowledged_date": event.acknowledged_date,
        "acknowledged_reaction": event.acknowledged_reaction,
        "episode_id": event.episode_id,
        "access": event.access,
        "access_reason": event.access_reason or "",
    }


def episode_dict(episode: models.HumanEpisode, card_names: dict[int, str]) -> dict:
    return {
        "id": episode.id,
        "created_date": episode.created_date,
        "last_event_date": episode.last_event_date,
        "closed_date": episode.closed_date,
        "open": episode.open,
        "events_count": episode.events_count,
        "best_event_id": episode.best_event_id,
        "matched": episode.matched,
        "matched_card_id": episode.matched_card_id,
        "matched_card_name": card_names.get(episode.matched_card_id) if episode.matched_card_id else None,
        "confidence": episode.confidence,
        "matched_lists": episode.matched_lists or [],
        "camera_groups": episode.camera_groups or [],
        "acknowledged": episode.acknowledged,
        "video_archive_id": episode.video_archive_id,
    }


def card_names(db: DbSession, items) -> dict[int, str]:
    ids = {i.matched_card_id for i in items if i.matched_card_id}
    if not ids:
        return {}
    return dict(db.execute(select(models.HumanCard.id, models.HumanCard.name).where(models.HumanCard.id.in_(ids))).all())


def _jsonable(data: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in data.items()}


def publish_event(db: DbSession, event: models.FaceEvent, created: bool) -> None:
    """Сообщает подписчикам о новом или изменённом событии и о его эпизоде. Вызывать после commit."""
    data = _jsonable(event_dict(event, card_names(db, [event])))
    broadcaster.publish("face_event_created" if created else "face_event_updated", data)
    if created:
        webhooks.dispatch(db, "face_event_created", data)
    if event.episode_id:
        episode = db.get(models.HumanEpisode, event.episode_id)
        if episode is not None:
            publish_episode(db, episode)


def publish_episode(db: DbSession, episode: models.HumanEpisode) -> None:
    broadcaster.publish("human_episode_updated", _jsonable(episode_dict(episode, card_names(db, [episode]))))


# --- Удаление ---
def delete_events(db: DbSession, *conditions) -> int:
    """Удаляет события по условию вместе с файлами. Фиксация транзакции — на вызывающем."""
    ids = list(db.scalars(select(models.FaceEvent.id).where(*conditions)))
    for start in range(0, len(ids), 500):
        db.execute(delete(models.FaceEvent).where(models.FaceEvent.id.in_(ids[start:start + 500])))
    for event_id in ids:
        storage.delete_event_files(event_id)
    return len(ids)


def delete_video_results(db: DbSession, video_archive_id: int) -> int:
    """Удаляет события и эпизоды, созданные по видеофайлу."""
    removed = delete_events(db, models.FaceEvent.video_archive_id == video_archive_id)
    db.execute(delete(models.HumanEpisode).where(models.HumanEpisode.video_archive_id == video_archive_id))
    return removed

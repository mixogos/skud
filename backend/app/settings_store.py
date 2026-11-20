"""Общие настройки, которые администратор меняет без перезапуска сервера."""
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import config, models


class RuntimeSettings(BaseModel):
    face_confidence_threshold: float = Field(
        default=config.CONFIDENCE_THRESHOLD, ge=0, le=1,
        description="Общий порог схожести: выше него лицо считается совпавшим с карточкой",
    )
    unmatched_events_enabled: bool = Field(default=True, description="Создавать события для лиц без совпадения с картотекой")
    unmatched_acknowledge: bool = Field(default=False, description="Требовать принятия событий без совпадения")
    liveness_threshold: float = Field(
        default=0.5, ge=0, le=1,
        description="Порог живости: ниже него лицо считается подделкой (фото или экран)",
    )
    dedup_window: int = Field(
        default=10, ge=0, le=3600,
        description="Удержание человека: столько секунд повторное появление того же лица не создаёт нового события (0 — выкл)",
    )
    unmatched_liveness_filter: bool = Field(
        default=True, description="Отсеивать картины и фото среди событий без совпадения по оценке живости",
    )
    unmatched_liveness_min: float = Field(
        default=0.35, ge=0, le=1,
        description="Порог живости для отсева картин и фото (события без совпадения с картотекой)",
    )
    episode_event_timeout: int = Field(
        default=30, ge=1, le=3600, description="Эпизод закрывается, если столько секунд нет новых событий"
    )
    episode_max_duration: int = Field(default=300, ge=1, le=86400, description="Наибольшая длительность эпизода, секунд")


def get_settings(db: DbSession) -> RuntimeSettings:
    stored = {row.key: row.value["v"] for row in db.scalars(select(models.Setting))}
    known = {k: v for k, v in stored.items() if k in RuntimeSettings.model_fields}
    return RuntimeSettings(**known)


def update_settings(db: DbSession, changes: dict) -> RuntimeSettings:
    """Проверяет и сохраняет изменения. Фиксация транзакции — на вызывающем."""
    merged = RuntimeSettings(**{**get_settings(db).model_dump(), **changes})
    for key in changes:
        row = db.get(models.Setting, key)
        value = {"v": getattr(merged, key)}
        if row is None:
            db.add(models.Setting(key=key, value=value))
        else:
            row.value = value
    return merged

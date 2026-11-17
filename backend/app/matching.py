"""Сопоставление обнаруженного лица с картотекой."""
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import models
from .recognition.index import face_index
from .settings_store import RuntimeSettings


@dataclass
class Match:
    card: models.HumanCard
    face_id: int
    confidence: float
    watch_lists: list[models.WatchList]  # активные списки карточки, чей порог пройден


def generic_threshold(group: models.CameraGroup | None, settings: RuntimeSettings) -> float:
    """Порог по умолчанию: у группы камер, если задан, иначе общий."""
    if group is not None and group.face_threshold is not None:
        return group.face_threshold
    return settings.face_confidence_threshold


def find_match(
    db: DbSession,
    vector: np.ndarray,
    when: datetime,
    group: models.CameraGroup | None,
    settings: RuntimeSettings,
) -> Match | None:
    """Ищет самую похожую действующую карточку из активных списков наблюдения.

    Порог: индивидуальный порог списка → порог группы камер → общий порог.
    Карточка учитывается, если она активна и момент события попадает в её период действия.
    """
    generic = generic_threshold(group, settings)
    active_lists = {wl.id: wl for wl in db.scalars(select(models.WatchList).where(models.WatchList.active))}
    if not active_lists:
        return None

    def threshold_of(wl: models.WatchList) -> float:
        return wl.face_threshold if wl.face_threshold is not None else generic

    floor = min(threshold_of(wl) for wl in active_lists.values())
    candidates = face_index.search_cards(db, vector, floor)
    for card_id, (confidence, face_id) in sorted(candidates.items(), key=lambda kv: kv[1][0], reverse=True):
        card = db.get(models.HumanCard, card_id)
        if card is None or not card.active:
            continue
        if card.active_after and when < card.active_after:
            continue
        if card.active_before and when >= card.active_before:
            continue
        passed = [wl for wl in card.watch_lists if wl.id in active_lists and confidence >= threshold_of(wl)]
        if passed:
            return Match(card, face_id, confidence, sorted(passed, key=lambda wl: wl.id))
    return None

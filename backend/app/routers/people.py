"""События по людям (силуэтам): просмотр, фильтры, снимок."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import models, storage
from ..db import get_db
from ..deps import require
from ..schemas import Page

router = APIRouter(tags=["События людей"])


class PersonEventOut(BaseModel):
    id: int
    created_date: datetime
    camera_group_id: int | None
    camera_id: int | None
    track_duration: float
    track_frames: int
    bbox: list[int]
    frame_width: int
    frame_height: int
    detection_score: float
    gender: str | None
    gender_confidence: float | None
    attrs: dict
    upper_color: str | None
    lower_color: str | None


def _naive(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@router.get("/events/people/", response_model=Page[PersonEventOut])
def list_person_events(
    camera_groups: list[int] | None = Query(default=None),
    cameras: list[int] | None = Query(default=None),
    gender: str | None = Query(default=None, description="male или female"),
    upper_color: str | None = None,
    lower_color: str | None = None,
    has: str | None = Query(default=None, description="признак: hat, bag, coat, longsleeves, longpants, longhair"),
    created_date_gte: datetime | None = None,
    created_date_lte: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("faceevent:view")),
):
    E = models.PersonEvent
    conds = []
    if camera_groups:
        conds.append(E.camera_group_id.in_(camera_groups))
    if cameras:
        conds.append(E.camera_id.in_(cameras))
    if gender in ("male", "female"):
        conds.append(E.gender == gender)
    if upper_color:
        conds.append(E.upper_color == upper_color)
    if lower_color:
        conds.append(E.lower_color == lower_color)
    if has:
        conds.append(func.json_extract(E.attrs, f"$.{has}") >= 0.5)
    if created_date_gte:
        conds.append(E.created_date >= _naive(created_date_gte))
    if created_date_lte:
        conds.append(E.created_date <= _naive(created_date_lte))
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    count = db.scalar(select(func.count()).select_from(E).where(*conds))
    rows = db.scalars(select(E).where(*conds).order_by(E.created_date.desc(), E.id.desc()).limit(limit).offset(offset)).all()
    return {"count": count, "results": [PersonEventOut.model_validate(r, from_attributes=True) for r in rows]}


@router.get("/events/people/{event_id}/image/")
def person_event_image(event_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("faceevent:view"))):
    """Снимок человека (JPEG)."""
    path = storage.person_image(event_id) if db.get(models.PersonEvent, event_id) is not None else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Снимок не найден")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

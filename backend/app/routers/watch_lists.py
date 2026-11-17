from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import access, audit, models
from ..db import get_db
from ..deps import require
from ..schemas import Page

router = APIRouter(prefix="/watch-lists", tags=["Списки наблюдения"])

_COLOR = r"^#[0-9a-fA-F]{6}$"
_POLICY = r"^(none|allow|deny)$"


class _ScheduleMixin(BaseModel):
    @field_validator("access_schedule", check_fields=False)
    @classmethod
    def _schedule(cls, value):
        return value if value is None else access.validate_schedule(value)


class WatchListCreate(_ScheduleMixin):
    name: str = Field(min_length=1, max_length=256)
    comment: str = Field(default="", max_length=2048)
    color: str = Field(default="#3d7eff", pattern=_COLOR)
    active: bool = True
    face_threshold: float | None = Field(default=None, ge=0, le=1)
    acknowledge: bool = False
    notify: bool = False
    ignore_events: bool = False
    access_policy: str = Field(default='none', pattern=_POLICY)
    access_schedule: dict = {}


class WatchListUpdate(_ScheduleMixin):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    comment: str | None = Field(default=None, max_length=2048)
    color: str | None = Field(default=None, pattern=_COLOR)
    active: bool | None = None
    face_threshold: float | None = Field(default=None, ge=0, le=1)
    acknowledge: bool | None = None
    notify: bool | None = None
    ignore_events: bool | None = None
    access_policy: str | None = Field(default=None, pattern=_POLICY)
    access_schedule: dict | None = None


class WatchListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    comment: str
    color: str
    active: bool
    face_threshold: float | None
    acknowledge: bool
    notify: bool
    ignore_events: bool
    access_policy: str
    access_schedule: dict
    predefined: bool
    created_date: datetime
    modified_date: datetime


def _get(db: DbSession, wl_id: int) -> models.WatchList:
    wl = db.get(models.WatchList, wl_id)
    if wl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Список наблюдения не найден")
    return wl


def _check_name(db: DbSession, name: str, exclude_id: int | None = None) -> None:
    other = db.scalar(select(models.WatchList).where(models.WatchList.name == name))
    if other and other.id != exclude_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Список наблюдения с таким названием уже существует")


@router.get("/", response_model=Page[WatchListOut])
def list_watch_lists(db: DbSession = Depends(get_db), _: models.User = Depends(require("watchlist:view"))):
    rows = db.scalars(select(models.WatchList).order_by(models.WatchList.id)).all()
    return {"count": len(rows), "results": rows}


@router.post("/", response_model=WatchListOut, status_code=status.HTTP_201_CREATED)
def create_watch_list(
    data: WatchListCreate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("watchlist:add")),
):
    _check_name(db, data.name)
    wl = models.WatchList(**data.model_dump())
    db.add(wl)
    db.flush()
    audit.log(db, request, actor, "create", "watchlist", wl.id, {"name": wl.name})
    db.commit()
    return wl


@router.get("/{wl_id}/", response_model=WatchListOut)
def get_watch_list(wl_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("watchlist:view"))):
    return _get(db, wl_id)


@router.patch("/{wl_id}/", response_model=WatchListOut)
def update_watch_list(
    wl_id: int,
    data: WatchListUpdate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("watchlist:change")),
):
    wl = _get(db, wl_id)
    # exclude_unset: явный face_threshold=null сбрасывает индивидуальный порог.
    fields = data.model_dump(exclude_unset=True)
    for key in list(fields):
        if fields[key] is None and key != "face_threshold":
            del fields[key]
    if "name" in fields and fields["name"] != wl.name:
        _check_name(db, fields["name"], exclude_id=wl.id)
    for key, value in fields.items():
        setattr(wl, key, value)
    if fields:
        audit.log(db, request, actor, "update", "watchlist", wl.id, fields)
        db.commit()
    return wl


@router.delete("/{wl_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_watch_list(
    wl_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("watchlist:delete")),
):
    wl = _get(db, wl_id)
    if wl.predefined:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Предустановленный список наблюдения нельзя удалить")
    used = db.scalar(
        select(func.count()).select_from(models.card_watch_lists).where(models.card_watch_lists.c.watch_list_id == wl.id)
    )
    if used:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"В списке есть карточки ({used}). Сначала перенесите или удалите их.",
        )
    audit.log(db, request, actor, "delete", "watchlist", wl.id, {"name": wl.name})
    db.delete(wl)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

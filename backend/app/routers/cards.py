"""Картотека: карточки людей, фото лиц в карточках, поиск по фото, пакетная загрузка."""
from datetime import datetime, timezone
from pathlib import PurePath
from types import SimpleNamespace
from typing import Literal

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import access, audit, config, models, storage
from ..db import get_db
from ..deps import require
from ..recognition import engine as eng
from ..recognition.index import face_index
from ..schemas import Page
from ..settings_store import get_settings
from .recognition import (
    FACEEVENT_PREFIX,
    detect_faces,
    engine_dep,
    read_photo,
    resolve_detection,
    resolve_embedding,
)

router = APIRouter(tags=["Картотека"])

BATCH_MAX_FILES = 100


# --- Схемы ---
class _ScheduleMixin(BaseModel):
    @field_validator("access_schedule", check_fields=False)
    @classmethod
    def _schedule(cls, value):
        return value if value is None else access.validate_schedule(value)


class CardCreate(_ScheduleMixin):
    name: str = Field(min_length=1, max_length=256)
    comment: str = Field(default="", max_length=2048)
    active: bool = True
    watch_lists: list[int] = Field(min_length=1)
    meta: dict = {}
    active_after: datetime | None = None
    active_before: datetime | None = None
    access_schedule: dict = {}


class CardUpdate(_ScheduleMixin):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    comment: str | None = Field(default=None, max_length=2048)
    active: bool | None = None
    watch_lists: list[int] | None = Field(default=None, min_length=1)
    meta: dict | None = None
    active_after: datetime | None = None
    active_before: datetime | None = None
    access_schedule: dict | None = None


class CardOut(BaseModel):
    id: int
    name: str
    comment: str
    active: bool
    filled: bool  # есть ли в карточке хотя бы одно фото лица
    watch_lists: list[int]
    meta: dict
    active_after: datetime | None
    active_before: datetime | None
    access_schedule: dict
    face_objects: int
    main_face: int | None  # id фото лица для миниатюры карточки
    created_date: datetime
    modified_date: datetime
    looks_like_confidence: float | None = None


class FaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    card_id: int
    bbox: list[int]
    detection_score: float
    has_source_photo: bool
    created_date: datetime


class BatchItem(BaseModel):
    file: str
    status: Literal["created", "error"]
    card: int | None = None
    error: str | None = None


class BatchOut(BaseModel):
    created: int
    failed: int
    results: list[BatchItem]


# --- Вспомогательное ---
def card_out(card: models.HumanCard, looks_like: float | None = None) -> CardOut:
    return CardOut(
        id=card.id,
        name=card.name,
        comment=card.comment,
        active=card.active,
        filled=bool(card.faces),
        watch_lists=sorted(wl.id for wl in card.watch_lists),
        meta=card.meta or {},
        active_after=card.active_after,
        active_before=card.active_before,
        access_schedule=card.access_schedule or {},
        face_objects=len(card.faces),
        main_face=card.faces[0].id if card.faces else None,
        created_date=card.created_date,
        modified_date=card.modified_date,
        looks_like_confidence=round(looks_like, 4) if looks_like is not None else None,
    )


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _get_card(db: DbSession, card_id: int) -> models.HumanCard:
    card = db.get(models.HumanCard, card_id)
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Карточка не найдена")
    return card


def _get_face(db: DbSession, face_id: int) -> models.FaceObject:
    face = db.get(models.FaceObject, face_id)
    if face is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фото лица не найдено")
    return face


def _load_watch_lists(db: DbSession, ids: list[int]) -> list[models.WatchList]:
    ids = sorted(set(ids))
    found = db.scalars(select(models.WatchList).where(models.WatchList.id.in_(ids))).all()
    missing = sorted(set(ids) - {wl.id for wl in found})
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Списки наблюдения не найдены: {missing}")
    return list(found)


def _check_period(after: datetime | None, before: datetime | None) -> None:
    if after and before and after >= before:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "active_after должно быть раньше active_before")


def add_face(
    db: DbSession,
    card: models.HumanCard,
    embedding: np.ndarray,
    bbox,
    detection_score: float,
    thumbnail: bytes,
    source_jpeg: bytes | None,
) -> models.FaceObject:
    face = models.FaceObject(
        card_id=card.id,
        embedding=np.asarray(embedding, dtype=np.float32).tobytes(),
        bbox=[int(v) for v in bbox],
        detection_score=float(detection_score),
        has_source_photo=source_jpeg is not None,
    )
    db.add(face)
    db.flush()
    storage.delete_face_files(face.id)  # SQLite может повторно выдать id удалённой записи
    storage.save_face_file(face.id, "thumb", thumbnail)
    if source_jpeg is not None:
        storage.save_face_file(face.id, "src", source_jpeg)
    return face


# --- Карточки ---
@router.get("/cards/humans/", response_model=Page[CardOut])
def list_cards(
    name_contains: str | None = None,
    watch_lists: list[int] | None = Query(default=None, description="Карточки из любого из этих списков"),
    active: bool | None = None,
    filled: bool | None = None,
    looks_like: str | None = Query(default=None, description="Поиск по лицу: detection:<id> или faceobject:<id>"),
    threshold: float | None = Query(default=None, ge=0, le=1, description="Порог схожести для looks_like"),
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    user: models.User = Depends(require("humancard:view")),
):
    C = models.HumanCard
    conds = []
    if name_contains:
        conds.append(C.name.icontains(name_contains, autoescape=True))
    if active is not None:
        conds.append(C.active == active)
    if watch_lists:
        conds.append(C.watch_lists.any(models.WatchList.id.in_(watch_lists)))
    if filled is not None:
        conds.append(C.faces.any() if filled else ~C.faces.any())
    limit, offset = max(1, min(limit, 500)), max(0, offset)

    if looks_like:
        vector = resolve_embedding(db, user, looks_like)
        matches = face_index.search_cards(
            db, vector, get_settings(db).face_confidence_threshold if threshold is None else threshold
        )
        if not matches:
            return {"count": 0, "results": []}
        cards = db.scalars(select(C).where(C.id.in_(matches), *conds)).all()
        cards = sorted(cards, key=lambda c: matches[c.id][0], reverse=True)
        page = cards[offset:offset + limit]
        return {"count": len(cards), "results": [card_out(c, matches[c.id][0]) for c in page]}

    count = db.scalar(select(func.count()).select_from(C).where(*conds))
    rows = db.scalars(select(C).where(*conds).order_by(C.id.desc()).limit(limit).offset(offset)).all()
    return {"count": count, "results": [card_out(c) for c in rows]}


@router.post("/cards/humans/", response_model=CardOut, status_code=status.HTTP_201_CREATED)
def create_card(
    data: CardCreate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("humancard:add")),
):
    after, before = _naive_utc(data.active_after), _naive_utc(data.active_before)
    _check_period(after, before)
    card = models.HumanCard(
        name=data.name,
        comment=data.comment,
        active=data.active,
        meta=data.meta,
        active_after=after,
        active_before=before,
        access_schedule=data.access_schedule,
        watch_lists=_load_watch_lists(db, data.watch_lists),
    )
    db.add(card)
    db.flush()
    audit.log(db, request, actor, "create", "humancard", card.id, {"name": card.name})
    db.commit()
    return card_out(card)


@router.post("/cards/humans/batch/", response_model=BatchOut)
def batch_upload(
    request: Request,
    photos: list[UploadFile] = File(description=f"До {BATCH_MAX_FILES} фото за один запрос"),
    watch_lists: list[int] = Form(),
    on_multiple: Literal["reject", "biggest"] = Form(
        default="reject", description="Если на фото несколько лиц: отклонить фото или взять самое крупное"
    ),
    name_prefix: str = Form(default="", max_length=64),
    comment: str = Form(default="", max_length=2048),
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("batchupload_cards", "humancard:add", "faceobject:add")),
    engine: eng.FaceEngine = Depends(engine_dep),
):
    """Пакетная загрузка: из каждого фото создаётся карточка, имя берётся из имени файла."""
    if len(photos) > BATCH_MAX_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не больше {BATCH_MAX_FILES} фото за один запрос")
    lists = _load_watch_lists(db, watch_lists)
    results: list[BatchItem] = []
    for photo in photos:
        filename = PurePath(photo.filename or "photo").name
        try:
            image = eng.decode_image(photo.file.read(config.MAX_PHOTO_BYTES + 1))
        except eng.BadImage as exc:
            results.append(BatchItem(file=filename, status="error", error=str(exc)))
            continue
        found = detect_faces(engine, image)
        if not found:
            results.append(BatchItem(file=filename, status="error", error="Лицо не найдено"))
            continue
        if len(found) > 1 and on_multiple == "reject":
            results.append(BatchItem(file=filename, status="error", error=f"На фото несколько лиц ({len(found)})"))
            continue
        det = found[0][0]  # самое крупное
        name = (name_prefix + PurePath(filename).stem)[:256] or "Без имени"
        card = models.HumanCard(name=name, comment=comment, watch_lists=list(lists))
        db.add(card)
        db.flush()
        add_face(db, card, det.embedding, det.bbox, det.detection_score, det.thumbnail, eng.encode_jpeg(image))
        results.append(BatchItem(file=filename, status="created", card=card.id))
    created = sum(r.status == "created" for r in results)
    audit.log(
        db, request, actor, "batch_upload", "humancard", "",
        {"created": created, "failed": len(results) - created, "watch_lists": sorted(wl.id for wl in lists)},
    )
    db.commit()
    face_index.invalidate()
    return BatchOut(created=created, failed=len(results) - created, results=results)


@router.get("/cards/humans/{card_id}/", response_model=CardOut)
def get_card(card_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("humancard:view"))):
    return card_out(_get_card(db, card_id))


@router.patch("/cards/humans/{card_id}/", response_model=CardOut)
def update_card(
    card_id: int,
    data: CardUpdate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("humancard:change")),
):
    card = _get_card(db, card_id)
    fields = data.model_dump(exclude_unset=True)
    # Явный null допустим только для границ периода действия (снимает ограничение).
    for key in list(fields):
        if fields[key] is None and key not in ("active_after", "active_before"):
            del fields[key]
    for key in ("active_after", "active_before"):
        if key in fields:
            fields[key] = _naive_utc(fields[key])
    _check_period(fields.get("active_after", card.active_after), fields.get("active_before", card.active_before))
    if "watch_lists" in fields:
        card.watch_lists = _load_watch_lists(db, fields["watch_lists"])
    for key in ("name", "comment", "active", "meta", "active_after", "active_before", "access_schedule"):
        if key in fields:
            setattr(card, key, fields[key])
    if fields:
        details = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in fields.items() if k not in ("meta", "access_schedule")}
        audit.log(db, request, actor, "update", "humancard", card.id, details)
        db.commit()
    return card_out(card)


@router.delete("/cards/humans/{card_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_card(
    card_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("humancard:delete")),
):
    card = _get_card(db, card_id)
    face_ids = [f.id for f in card.faces]
    audit.log(db, request, actor, "delete", "humancard", card.id, {"name": card.name, "face_objects": len(face_ids)})
    db.delete(card)
    db.commit()
    for face_id in face_ids:
        storage.delete_face_files(face_id)
    face_index.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Фото лиц в карточках ---
@router.get("/objects/faces/", response_model=Page[FaceOut])
def list_faces(
    card: int | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("faceobject:view")),
):
    F = models.FaceObject
    conds = [F.card_id == card] if card is not None else []
    count = db.scalar(select(func.count()).select_from(F).where(*conds))
    rows = db.scalars(
        select(F).where(*conds).order_by(F.id).limit(max(1, min(limit, 500))).offset(max(0, offset))
    ).all()
    return {"count": count, "results": rows}


@router.post("/objects/faces/", response_model=FaceOut, status_code=status.HTTP_201_CREATED)
def create_face(
    request: Request,
    card: int = Form(description="id карточки"),
    source_photo: UploadFile | None = File(default=None, description="Фото с лицом"),
    create_from: str | None = Form(
        default=None,
        description="detection:<id> из /detect/ (если на фото несколько лиц) или faceevent:<id> — лицо из события",
    ),
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("faceobject:add")),
):
    """Добавляет фото лица в карточку.

    Передайте source_photo с одним лицом, либо create_from с результатом /detect/
    (можно вместе с source_photo, чтобы сохранить исходное фото).
    """
    card_obj = _get_card(db, card)
    source_jpeg = None
    if create_from and create_from.startswith(FACEEVENT_PREFIX):
        # Создание или обогащение карточки из события: берём лицо и кадр события.
        if not actor.has_perm("faceevent:view"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав: faceevent:view")
        raw = create_from[len(FACEEVENT_PREFIX):]
        event = db.get(models.FaceEvent, int(raw)) if raw.isdigit() else None
        thumb = storage.event_file(event.id, "thumb") if event else None
        if event is None or thumb is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Объект {create_from} не найден")
        full = storage.event_file(event.id, "full")
        det = SimpleNamespace(
            embedding=np.frombuffer(event.embedding, dtype=np.float32),
            bbox=event.bbox,
            detection_score=event.detection_score,
            thumbnail=thumb.read_bytes(),
        )
        source_jpeg = full.read_bytes() if full else None
    elif create_from:
        det = resolve_detection(create_from)
        if source_photo is not None:
            source_jpeg = eng.encode_jpeg(read_photo(source_photo))
    elif source_photo is not None:
        image = read_photo(source_photo)
        found = detect_faces(engine_dep(), image)
        if not found:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "На фото не найдено лицо")
        if len(found) > 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {
                    "message": "На фото несколько лиц — укажите нужное в create_from",
                    "faces": [out.model_dump() for _, out in found],
                },
            )
        det = found[0][0]
        source_jpeg = eng.encode_jpeg(image)
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нужно передать source_photo или create_from")

    face = add_face(db, card_obj, det.embedding, det.bbox, det.detection_score, det.thumbnail, source_jpeg)
    audit.log(db, request, actor, "create", "faceobject", face.id, {"card": card_obj.id})
    db.commit()
    face_index.invalidate()
    return face


@router.get("/objects/faces/{face_id}/", response_model=FaceOut)
def get_face(face_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("faceobject:view"))):
    return _get_face(db, face_id)


def _face_file_response(db: DbSession, face_id: int, kind: str) -> FileResponse:
    _get_face(db, face_id)
    path = storage.face_file(face_id, kind)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/objects/faces/{face_id}/thumbnail/", response_class=FileResponse)
def face_thumbnail(face_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("faceobject:view"))):
    return _face_file_response(db, face_id, "thumb")


@router.get("/objects/faces/{face_id}/source_photo/", response_class=FileResponse)
def face_source_photo(face_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("faceobject:view"))):
    return _face_file_response(db, face_id, "src")


@router.delete("/objects/faces/{face_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_face(
    face_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("faceobject:delete")),
):
    face = _get_face(db, face_id)
    audit.log(db, request, actor, "delete", "faceobject", face.id, {"card": face.card_id})
    db.delete(face)
    db.commit()
    storage.delete_face_files(face_id)
    face_index.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

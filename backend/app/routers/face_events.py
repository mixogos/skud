"""События распознавания лиц и эпизоды: просмотр, фильтры, поиск по лицу, принятие, лента."""
import asyncio
from datetime import datetime

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session as DbSession

from .. import audit, episodes, events, models, security, storage
from ..db import SessionLocal, get_db
from ..deps import auth_bypassed, require
from ..notifications import broadcaster
from ..schemas import Page
from ..settings_store import get_settings
from .recognition import resolve_embedding

router = APIRouter(tags=["События и эпизоды"])

SEARCH_SCAN_LIMIT = 200_000  # сколько последних событий просматривает поиск по лицу


class FaceEventOut(BaseModel):
    id: int
    created_date: datetime
    camera_group_id: int | None
    camera_id: int | None
    video_archive_id: int | None
    video_offset: float | None
    track_duration: float
    track_frames: int
    bbox: list[int]
    frame_width: int
    frame_height: int
    detection_score: float
    quality: float
    features: dict
    liveness: float | None = None
    matched: bool
    matched_card_id: int | None
    matched_card_name: str | None
    matched_face_id: int | None
    confidence: float | None
    matched_lists: list[int]
    acknowledged: bool
    acknowledged_by: int | None
    acknowledged_date: datetime | None
    acknowledged_reaction: str
    episode_id: int | None
    access: str | None
    access_reason: str
    looks_like_confidence: float | None = None


class EpisodeOut(BaseModel):
    id: int
    created_date: datetime
    last_event_date: datetime
    closed_date: datetime | None
    open: bool
    events_count: int
    best_event_id: int | None
    matched: bool
    matched_card_id: int | None
    matched_card_name: str | None
    confidence: float | None
    matched_lists: list[int]
    camera_groups: list[int]
    acknowledged: bool
    video_archive_id: int | None


class AcknowledgeIn(BaseModel):
    acknowledged: bool = True
    acknowledged_reaction: str = Field(default="", max_length=64)


def _naive(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value else None


def _in_json_list(column, value: int):
    """Условие «JSON-массив в колонке содержит значение» (SQLite)."""
    items = func.json_each(column).table_valued("value")
    return select(1).select_from(items).where(items.c.value == value).exists()


def _get_event(db: DbSession, event_id: int) -> models.FaceEvent:
    event = db.get(models.FaceEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Событие не найдено")
    return event


def _get_episode(db: DbSession, episode_id: int) -> models.HumanEpisode:
    episode = db.get(models.HumanEpisode, episode_id)
    if episode is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Эпизод не найден")
    return episode


def _set_acknowledged(event: models.FaceEvent, value: bool, reaction: str, user: models.User) -> None:
    event.acknowledged = value
    event.acknowledged_by = user.id if value else None
    event.acknowledged_date = models.utcnow() if value else None
    event.acknowledged_reaction = reaction if value else ""


# --- События ---
@router.get("/events/faces/", response_model=Page[FaceEventOut])
def list_events(
    matched: bool | None = Query(default=None, description="Только с совпадением / только без совпадения"),
    watch_lists: list[int] | None = Query(default=None, description="События по любому из этих списков наблюдения"),
    acknowledged: bool | None = None,
    camera_groups: list[int] | None = Query(default=None),
    cameras: list[int] | None = Query(default=None),
    video_archive: int | None = None,
    matched_card: int | None = None,
    card_name_contains: str | None = None,
    episode: int | None = None,
    access: str | None = Query(default=None, description='Решение о доступе: granted или denied'),
    liveness: str | None = Query(default=None, description="Живость: real — настоящее лицо, fake — подозрение на подделку"),
    gender: str | None = Query(default=None, description="Пол: male или female"),
    age_gte: int | None = Query(default=None, ge=0, le=100, description="Возраст не меньше"),
    age_lte: int | None = Query(default=None, ge=0, le=100, description="Возраст не больше"),
    medmask: str | None = Query(default=None, description="Маска: correct — в маске, none — без маски"),
    id_in: list[int] | None = Query(default=None, description="События с указанными id"),
    created_date_gte: datetime | None = None,
    created_date_lte: datetime | None = None,
    looks_like: str | None = Query(default=None, description="Поиск по лицу: detection:, faceobject: или faceevent:<id>"),
    threshold: float | None = Query(default=None, ge=0, le=1, description="Порог схожести для looks_like"),
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    user: models.User = Depends(require("faceevent:view")),
):
    E = models.FaceEvent
    conds = []
    if matched is not None:
        conds.append(E.matched == matched)
    if watch_lists:
        conds.append(or_(*[_in_json_list(E.matched_lists, wl) for wl in watch_lists]))
    if acknowledged is not None:
        conds.append(E.acknowledged == acknowledged)
    if camera_groups:
        conds.append(E.camera_group_id.in_(camera_groups))
    if cameras:
        conds.append(E.camera_id.in_(cameras))
    if video_archive is not None:
        conds.append(E.video_archive_id == video_archive)
    if matched_card is not None:
        conds.append(E.matched_card_id == matched_card)
    if card_name_contains:
        conds.append(
            E.matched_card_id.in_(
                select(models.HumanCard.id).where(models.HumanCard.name.icontains(card_name_contains, autoescape=True))
            )
        )
    if episode is not None:
        conds.append(E.episode_id == episode)
    if access:
        conds.append(E.access == access)
    if liveness in ("real", "fake"):
        limit_value = get_settings(db).liveness_threshold
        conds.append(E.liveness >= limit_value if liveness == "real" else E.liveness < limit_value)
    if gender in ("male", "female"):
        conds.append(func.json_extract(E.features, "$.gender.name") == gender)
    if age_gte is not None:
        conds.append(func.json_extract(E.features, "$.age") >= age_gte)
    if age_lte is not None:
        conds.append(func.json_extract(E.features, "$.age") <= age_lte)
    if medmask in ("correct", "none"):
        conds.append(func.json_extract(E.features, "$.medmask.name") == medmask)
    if id_in:
        conds.append(E.id.in_(id_in))
    if created_date_gte:
        conds.append(E.created_date >= _naive(created_date_gte))
    if created_date_lte:
        conds.append(E.created_date <= _naive(created_date_lte))
    limit, offset = max(1, min(limit, 500)), max(0, offset)

    if looks_like:
        vector = resolve_embedding(db, user, looks_like)
        minimum = get_settings(db).face_confidence_threshold if threshold is None else threshold
        rows = db.execute(select(E.id, E.embedding).where(*conds).order_by(E.id.desc()).limit(SEARCH_SCAN_LIMIT)).all()
        if not rows:
            return {"count": 0, "results": []}
        matrix = np.stack([np.frombuffer(blob, dtype=np.float32) for _, blob in rows])
        conf = np.clip((matrix @ vector.astype(np.float32) + 1.0) / 2.0, 0.0, 1.0)
        order = [i for i in np.argsort(-conf) if conf[i] >= minimum]
        page = order[offset:offset + limit]
        found = {e.id: e for e in db.scalars(select(E).where(E.id.in_([rows[i][0] for i in page])))}
        names = events.card_names(db, found.values())
        results = [
            {**events.event_dict(found[rows[i][0]], names), "looks_like_confidence": round(float(conf[i]), 4)} for i in page
        ]
        return {"count": len(order), "results": results}

    count = db.scalar(select(func.count()).select_from(E).where(*conds))
    rows = db.scalars(select(E).where(*conds).order_by(E.created_date.desc(), E.id.desc()).limit(limit).offset(offset)).all()
    names = events.card_names(db, rows)
    return {"count": count, "results": [events.event_dict(e, names) for e in rows]}


@router.post("/events/faces/acknowledge/")
def acknowledge_all(
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("faceevent:change")),
):
    """Принимает все непринятые события разом."""
    E = models.FaceEvent
    result = db.execute(
        update(E).where(E.acknowledged.is_(False)).values(
            acknowledged=True, acknowledged_by=actor.id, acknowledged_date=models.utcnow()
        )
    )
    db.execute(update(models.HumanEpisode).where(models.HumanEpisode.acknowledged.is_(False)).values(acknowledged=True))
    audit.log(db, request, actor, "acknowledge_all", "faceevent", "", {"count": result.rowcount})
    db.commit()
    broadcaster.publish("events_acknowledged_all", {"count": result.rowcount, "user_id": actor.id})
    return {"acknowledged": result.rowcount}


@router.get("/events/faces/{event_id}/", response_model=FaceEventOut)
def get_event(event_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("faceevent:view"))):
    event = _get_event(db, event_id)
    return events.event_dict(event, events.card_names(db, [event]))


@router.patch("/events/faces/{event_id}/", response_model=FaceEventOut)
def acknowledge_event(
    event_id: int,
    data: AcknowledgeIn,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("faceevent:change")),
):
    """Принимает событие (или снимает принятие)."""
    event = _get_event(db, event_id)
    _set_acknowledged(event, data.acknowledged, data.acknowledged_reaction, actor)
    db.flush()
    if event.episode_id:
        episodes.refresh_acknowledged(db, event.episode_id)
    audit.log(db, request, actor, "acknowledge" if data.acknowledged else "unacknowledge", "faceevent", event.id)
    db.commit()
    events.publish_event(db, event, created=False)
    return events.event_dict(event, events.card_names(db, [event]))


def _file(db: DbSession, event_id: int, kind: str) -> FileResponse:
    _get_event(db, event_id)
    path = storage.event_file(event_id, kind)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/events/faces/{event_id}/thumbnail/", response_class=FileResponse)
def event_thumbnail(event_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("faceevent:view"))):
    """Миниатюра лица."""
    return _file(db, event_id, "thumb")


@router.get("/events/faces/{event_id}/fullframe/", response_class=FileResponse)
def event_fullframe(event_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("faceevent:view"))):
    """Полный кадр, на котором обнаружено лицо."""
    return _file(db, event_id, "full")


# --- Эпизоды ---
@router.get("/episodes/humans/", response_model=Page[EpisodeOut])
def list_episodes(
    open: bool | None = Query(default=None, description="Только LIVE-эпизоды / только закрытые"),
    matched: bool | None = None,
    watch_lists: list[int] | None = Query(default=None),
    acknowledged: bool | None = None,
    camera_groups: list[int] | None = Query(default=None),
    video_archive: int | None = None,
    matched_card: int | None = None,
    created_date_gte: datetime | None = None,
    created_date_lte: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("humanepisode:view")),
):
    # Эпизоды с камер закрываются по времени; проверяем это перед выдачей списка.
    episodes.close_stale(db, models.utcnow(), get_settings(db))
    db.commit()

    P = models.HumanEpisode
    conds = []
    if open is not None:
        conds.append(P.open == open)
    if matched is not None:
        conds.append(P.matched == matched)
    if watch_lists:
        conds.append(or_(*[_in_json_list(P.matched_lists, wl) for wl in watch_lists]))
    if camera_groups:
        conds.append(or_(*[_in_json_list(P.camera_groups, g) for g in camera_groups]))
    if acknowledged is not None:
        conds.append(P.acknowledged == acknowledged)
    if video_archive is not None:
        conds.append(P.video_archive_id == video_archive)
    if matched_card is not None:
        conds.append(P.matched_card_id == matched_card)
    if created_date_gte:
        conds.append(P.created_date >= _naive(created_date_gte))
    if created_date_lte:
        conds.append(P.created_date <= _naive(created_date_lte))
    count = db.scalar(select(func.count()).select_from(P).where(*conds))
    rows = db.scalars(
        select(P).where(*conds).order_by(P.last_event_date.desc(), P.id.desc()).limit(max(1, min(limit, 500))).offset(max(0, offset))
    ).all()
    names = events.card_names(db, rows)
    return {"count": count, "results": [events.episode_dict(e, names) for e in rows]}


@router.get("/episodes/humans/{episode_id}/", response_model=EpisodeOut)
def get_episode(episode_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("humanepisode:view"))):
    episode = _get_episode(db, episode_id)
    return events.episode_dict(episode, events.card_names(db, [episode]))


@router.patch("/episodes/humans/{episode_id}/", response_model=EpisodeOut)
def acknowledge_episode(
    episode_id: int,
    data: AcknowledgeIn,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("humanepisode:change", "faceevent:change")),
):
    """Принимает эпизод: принимаются все его события."""
    episode = _get_episode(db, episode_id)
    for event in db.scalars(select(models.FaceEvent).where(models.FaceEvent.episode_id == episode.id)):
        if event.acknowledged != data.acknowledged:
            _set_acknowledged(event, data.acknowledged, data.acknowledged_reaction, actor)
    episode.acknowledged = data.acknowledged
    audit.log(db, request, actor, "acknowledge" if data.acknowledged else "unacknowledge", "humanepisode", episode.id)
    db.commit()
    events.publish_episode(db, episode)
    return events.episode_dict(episode, events.card_names(db, [episode]))


# --- Лента в реальном времени ---
def _ws_user_allowed(token: str) -> bool:
    with SessionLocal() as db:
        session = db.scalar(select(models.Session).where(models.Session.token_hash == security.token_hash(token)))
        return bool(
            session
            and session.expires > models.utcnow()
            and session.user.active
            and session.user.has_perm("faceevent:view")
        )


@router.websocket("/ws/events/")
async def events_feed(websocket: WebSocket):
    """Лента событий. Первым сообщением клиент присылает {"token": "<токен>"};
    после ответа {"type": "ready"} сервер шлёт сообщения о событиях и эпизодах."""
    await websocket.accept()
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        token = hello.get("token") if isinstance(hello, dict) else None
        local = auth_bypassed(websocket.client.host if websocket.client else None)
        if not local and (not isinstance(token, str) or not await run_in_threadpool(_ws_user_allowed, token)):
            await websocket.close(code=4401, reason="Требуется авторизация")
            return
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=4401, reason="Требуется авторизация")
        return
    except WebSocketDisconnect:
        return

    queue = broadcaster.register()
    incoming = asyncio.ensure_future(websocket.receive_text())
    outgoing = asyncio.ensure_future(queue.get())
    try:
        await websocket.send_json({"type": "ready"})
        while True:
            done, _ = await asyncio.wait({incoming, outgoing}, return_when=asyncio.FIRST_COMPLETED)
            if incoming in done:
                incoming.result()  # исключение при разрыве соединения
                incoming = asyncio.ensure_future(websocket.receive_text())  # сообщения клиента игнорируются
            if outgoing in done:
                await websocket.send_json(outgoing.result())
                outgoing = asyncio.ensure_future(queue.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        broadcaster.unregister(queue)
        for task in (incoming, outgoing):
            task.cancel()

"""Камеры и внешние детекторы.

Камера — видеопоток (RTSP и др.), который сервер читает сам: в запросе на создание указан url.
Внешний детектор — терминал контроля доступа или другое устройство, которое само присылает
кадры: авторизуется собственным токеном и сразу получает результат и решение о доступе.
"""
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit, events, models, security
from ..db import get_db
from ..deps import require
from ..recognition import engine as eng
from ..schemas import Page
from ..video.analytics import delete_counter_images
from ..video.processor import TrackResult
from ..video.quality import face_quality
from ..video.streams import MASK, mask_url, stream_manager, validate_url
from ..video.tracker import StreamSettings
from .recognition import read_photo

router = APIRouter(tags=["Камеры и внешние детекторы"])


class DetectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    group_id: int
    # Адрес потока. Если указан — создаётся камера, иначе внешний детектор.
    url: str = Field(default="", max_length=1024)
    stream_settings: dict = Field(default_factory=dict)
    comment: str = Field(default="", max_length=2048)
    active: bool = True


class DetectorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    group_id: int | None = None
    url: str | None = Field(default=None, max_length=1024)
    stream_settings: dict | None = None
    comment: str | None = Field(default=None, max_length=2048)
    active: bool | None = None


class DetectorOut(BaseModel):
    id: int
    name: str
    group_id: int
    comment: str
    active: bool
    external_detector: bool
    url: str  # пароль в адресе скрыт
    stream_settings: dict
    state: str  # камера: DISABLED | CONNECTING | ONLINE | ERROR; детектор: READY
    state_message: str
    frames_processed: int = 0
    events_created: int = 0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    last_frame_date: datetime | None = None
    created_date: datetime
    modified_date: datetime
    token: str | None = None  # выдаётся только при создании и при замене токена


def _out(camera: models.Camera, token: str | None = None) -> DetectorOut:
    out = DetectorOut(
        id=camera.id, name=camera.name, group_id=camera.group_id, comment=camera.comment, active=camera.active,
        external_detector=camera.external_detector, url=mask_url(camera.url or ""),
        stream_settings=camera.stream_settings or {}, state=camera.state, state_message=camera.state_message or "",
        created_date=camera.created_date, modified_date=camera.modified_date, token=token,
    )
    stream = stream_manager.get(camera.id)
    if stream is not None:
        out.state, out.state_message = stream.state, stream.message
        out.frames_processed, out.events_created, out.fps = stream.frames_processed, stream.events_created, stream.fps
        out.width, out.height, out.last_frame_date = stream.width, stream.height, stream.last_frame_date
    return out


def _clean_url(url: str) -> str:
    try:
        return validate_url(url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _clean_settings(raw: dict) -> dict:
    try:
        return StreamSettings(**raw).model_dump(exclude_defaults=True)
    except (TypeError, ValidationError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Некорректные параметры обработки: {exc}")


class DetectorEventOut(BaseModel):
    event: int | None  # id созданного события
    matched: bool
    card: dict | None
    confidence: float | None
    watch_lists: list[int]
    access: str | None  # granted | denied | None (у группы камер не включён контроль доступа)
    reason: str
    quality: float | None
    liveness: float | None = None  # оценка живости лица, 0..1


def _get(db: DbSession, camera_id: int, external: bool | None = None) -> models.Camera:
    camera = db.get(models.Camera, camera_id)
    if camera is None or (external is not None and camera.external_detector != external):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Камера или детектор не найдены")
    return camera


def _check_group(db: DbSession, group_id: int) -> None:
    if db.get(models.CameraGroup, group_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Группа камер не найдена")


@router.get("/cameras/", response_model=Page[DetectorOut])
def list_cameras(
    external_detector: bool | None = None,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("camera:view")),
):
    """Камеры и внешние детекторы. external_detector=true|false оставляет только один вид."""
    query = select(models.Camera).order_by(models.Camera.id)
    if external_detector is not None:
        query = query.where(models.Camera.external_detector.is_(external_detector))
    rows = db.scalars(query).all()
    return {"count": len(rows), "results": [_out(row) for row in rows]}


@router.get("/cameras/{camera_id}/", response_model=DetectorOut)
def get_camera(camera_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("camera:view"))):
    return _out(_get(db, camera_id))


@router.get("/cameras/{camera_id}/snapshot/")
def camera_snapshot(camera_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("camera:view"))):
    """Последний кадр с камеры (JPEG)."""
    _get(db, camera_id, external=False)
    stream = stream_manager.get(camera_id)
    data = stream.snapshot() if stream is not None else None
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Кадров с камеры пока нет")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


MJPEG_FPS = 12  # сколько кадров в секунду отдавать в онлайн-видео


@router.get("/cameras/{camera_id}/mjpeg/")
def camera_mjpeg(camera_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("camera:view"))):
    """Живое видео с камеры (MJPEG, multipart/x-mixed-replace) — для онлайн-просмотра в браузере."""
    _get(db, camera_id, external=False)

    def frames():
        interval = 1.0 / MJPEG_FPS
        empty = 0
        while True:
            stream = stream_manager.get(camera_id)
            data = stream.snapshot(boxes=True) if stream is not None else None
            if data is None:
                empty += 1
                if empty > 200:  # ~20 секунд без кадров — закрываем поток
                    return
                time.sleep(0.1)
                continue
            empty = 0
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n" % (len(data), data)
            time.sleep(interval)

    return StreamingResponse(
        frames(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"}
    )


@router.post("/cameras/", response_model=DetectorOut, status_code=status.HTTP_201_CREATED)
def create_detector(
    data: DetectorCreate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("camera:add")),
):
    """Создаёт камеру (указан url) или внешний детектор (url пуст).

    Токен детектора показывается один раз — в ответе на этот запрос."""
    _check_group(db, data.group_id)
    token = None
    if data.url.strip():
        camera = models.Camera(
            name=data.name, group_id=data.group_id, comment=data.comment, active=data.active,
            url=_clean_url(data.url), stream_settings=_clean_settings(data.stream_settings),
        )
    else:
        token = security.new_token()
        camera = models.Camera(
            name=data.name, group_id=data.group_id, comment=data.comment, active=data.active,
            external_detector=True, token_hash=security.token_hash(token), state="READY",
        )
    db.add(camera)
    db.flush()
    audit.log(db, request, actor, "create", "camera", camera.id, {"name": camera.name})
    db.commit()
    stream_manager.sync(camera.id)
    db.refresh(camera)
    return _out(camera, token)


@router.patch("/cameras/{camera_id}/", response_model=DetectorOut)
def update_detector(
    camera_id: int,
    data: DetectorUpdate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("camera:change")),
):
    camera = _get(db, camera_id)
    fields = data.model_dump(exclude_none=True)
    if "group_id" in fields:
        _check_group(db, fields["group_id"])
    if camera.external_detector:
        if fields.get("url") or fields.get("stream_settings"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "У внешнего детектора нет адреса потока")
        fields.pop("url", None)
        fields.pop("stream_settings", None)
    else:
        if "url" in fields:
            # Интерфейс получает адрес со скрытым паролем; такой адрес означает «не менять».
            if MASK in fields["url"] and fields["url"].strip() == mask_url(camera.url):
                del fields["url"]
            else:
                fields["url"] = _clean_url(fields["url"])
        if "stream_settings" in fields:
            fields["stream_settings"] = _clean_settings(fields["stream_settings"])
    for key, value in fields.items():
        setattr(camera, key, value)
    if fields:
        logged = {**fields, "url": mask_url(fields["url"])} if "url" in fields else fields
        audit.log(db, request, actor, "update", "camera", camera.id, logged)
        db.commit()
        if not camera.external_detector:
            stream_manager.sync(camera.id)
            db.refresh(camera)
    return _out(camera)


@router.post("/cameras/{camera_id}/token/", response_model=DetectorOut)
def regenerate_token(
    camera_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("camera:change")),
):
    """Выдаёт новый токен; прежний сразу перестаёт действовать."""
    camera = _get(db, camera_id, external=True)
    token = security.new_token()
    camera.token_hash = security.token_hash(token)
    audit.log(db, request, actor, "regenerate_token", "camera", camera.id)
    db.commit()
    return _out(camera, token)


@router.delete("/cameras/{camera_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_detector(
    camera_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("camera:delete")),
):
    camera = _get(db, camera_id)
    audit.log(db, request, actor, "delete", "camera", camera.id, {"name": camera.name})
    # Счётчики и линии камеры удаляются вместе с ней; снимки записей — заранее.
    counter_ids = select(models.Counter.id).where(models.Counter.camera_id == camera.id)
    delete_counter_images(db, models.CounterRecord.counter_id.in_(counter_ids))
    db.delete(camera)
    db.commit()
    stream_manager.sync(camera_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def detector_by_token(request: Request, db: DbSession = Depends(get_db)) -> models.Camera:
    """Авторизация устройства: «Authorization: Token <токен детектора>»."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    camera = None
    if scheme.lower() == "token" and token.strip():
        camera = db.scalar(
            select(models.Camera).where(
                models.Camera.token_hash == security.token_hash(token.strip()), models.Camera.external_detector
            )
        )
    if camera is None or not camera.active:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Неверный токен детектора или детектор отключён",
            headers={"WWW-Authenticate": "Token"},
        )
    return camera


@router.post("/events/faces/add/", response_model=DetectorEventOut)
def add_event(
    fullframe: UploadFile = File(description="Кадр с лицом (JPEG, PNG или WEBP)"),
    timestamp: datetime | None = Form(default=None, description="Время кадра; по умолчанию — текущее"),
    camera: models.Camera = Depends(detector_by_token),
    db: DbSession = Depends(get_db),
):
    """Принимает кадр от внешнего детектора, распознаёт самое крупное лицо, создаёт событие
    и возвращает решение о доступе. Вызывается устройством, а не пользователем."""
    try:
        engine = eng.get_engine()
    except eng.ModelsMissing as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    image = read_photo(fullframe)
    group = camera.group
    faces = engine.detect(image)
    if not faces:
        return DetectorEventOut(
            event=None, matched=False, card=None, confidence=None, watch_lists=[],
            access="denied" if group.access_control else None, reason="Лицо не найдено", quality=None,
        )
    face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    engine.extract(image, [face])
    quality = face_quality(image, face)
    when = timestamp.astimezone(timezone.utc).replace(tzinfo=None) if timestamp and timestamp.tzinfo else timestamp
    when = when or models.utcnow()

    result = TrackResult(track_id=0, face=face, frame=image, quality=quality, time=0.0, first_time=0.0, duration=0.0, frames=1)
    reg = events.process_track(db, result, created_date=when, camera_group_id=group.id, camera_id=camera.id)
    db.commit()
    if reg.event is not None:
        events.publish_event(db, reg.event, created=True)
    return DetectorEventOut(
        event=reg.event.id if reg.event else None,
        matched=reg.match is not None,
        card={"id": reg.match.card.id, "name": reg.match.card.name} if reg.match else None,
        confidence=round(reg.match.confidence, 4) if reg.match else None,
        watch_lists=[wl.id for wl in reg.match.watch_lists] if reg.match else [],
        access=reg.access,
        reason=reg.access_reason or reg.skipped,
        quality=quality,
        liveness=result.liveness,
    )

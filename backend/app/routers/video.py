"""Источники видео: группы камер и видеофайлы."""
import json
import secrets
from datetime import datetime
from pathlib import PurePath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import audit, config, events, models, storage
from ..db import get_db
from ..deps import require
from ..schemas import Page
from ..video.processor import VideoError, probe_video
from ..video.tracker import StreamSettings
from ..video.worker import ACTIVE_STATES, video_worker

router = APIRouter(tags=["Источники видео"])


# --- Группы камер ---
class CameraGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    comment: str = Field(default="", max_length=2048)
    active: bool = True
    deduplicate: bool = False
    deduplicate_delay: int = Field(default=15, ge=1, le=3600)
    face_threshold: float | None = Field(default=None, ge=0, le=1)
    access_control: bool = False
    liveness_check: bool = False
    detect_people: bool = False


class CameraGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    comment: str | None = Field(default=None, max_length=2048)
    active: bool | None = None
    deduplicate: bool | None = None
    deduplicate_delay: int | None = Field(default=None, ge=1, le=3600)
    face_threshold: float | None = Field(default=None, ge=0, le=1)
    access_control: bool | None = None
    liveness_check: bool | None = None
    detect_people: bool | None = None


class CameraGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    comment: str
    active: bool
    deduplicate: bool
    deduplicate_delay: int
    face_threshold: float | None
    access_control: bool
    liveness_check: bool
    detect_people: bool
    predefined: bool
    created_date: datetime
    modified_date: datetime


def _get_group(db: DbSession, group_id: int) -> models.CameraGroup:
    group = db.get(models.CameraGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Группа камер не найдена")
    return group


def _check_group_name(db: DbSession, name: str, exclude_id: int | None = None) -> None:
    other = db.scalar(select(models.CameraGroup).where(models.CameraGroup.name == name))
    if other and other.id != exclude_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Группа камер с таким названием уже существует")


@router.get("/camera-groups/", response_model=Page[CameraGroupOut])
def list_camera_groups(db: DbSession = Depends(get_db), _: models.User = Depends(require("cameragroup:view"))):
    rows = db.scalars(select(models.CameraGroup).order_by(models.CameraGroup.id)).all()
    return {"count": len(rows), "results": rows}


@router.post("/camera-groups/", response_model=CameraGroupOut, status_code=status.HTTP_201_CREATED)
def create_camera_group(
    data: CameraGroupCreate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("cameragroup:add")),
):
    _check_group_name(db, data.name)
    group = models.CameraGroup(**data.model_dump())
    db.add(group)
    db.flush()
    audit.log(db, request, actor, "create", "cameragroup", group.id, {"name": group.name})
    db.commit()
    return group


@router.get("/camera-groups/{group_id}/", response_model=CameraGroupOut)
def get_camera_group(group_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("cameragroup:view"))):
    return _get_group(db, group_id)


@router.patch("/camera-groups/{group_id}/", response_model=CameraGroupOut)
def update_camera_group(
    group_id: int,
    data: CameraGroupUpdate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("cameragroup:change")),
):
    group = _get_group(db, group_id)
    fields = data.model_dump(exclude_unset=True)
    for key in list(fields):
        if fields[key] is None and key != "face_threshold":
            del fields[key]
    if "name" in fields and fields["name"] != group.name:
        _check_group_name(db, fields["name"], exclude_id=group.id)
    for key, value in fields.items():
        setattr(group, key, value)
    if fields:
        audit.log(db, request, actor, "update", "cameragroup", group.id, fields)
        db.commit()
    return group


@router.delete("/camera-groups/{group_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera_group(
    group_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("cameragroup:delete")),
):
    group = _get_group(db, group_id)
    if group.predefined:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Предустановленную группу камер нельзя удалить")
    videos = db.scalar(select(func.count()).select_from(models.VideoArchive).where(models.VideoArchive.camera_group_id == group.id))
    cameras = db.scalar(select(func.count()).select_from(models.Camera).where(models.Camera.group_id == group.id))
    if videos or cameras:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"В группе есть камеры ({cameras}) или видеофайлы ({videos}). Сначала перенесите или удалите их.",
        )
    audit.log(db, request, actor, "delete", "cameragroup", group.id, {"name": group.name})
    db.delete(group)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Видеофайлы ---
class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    camera_group_id: int
    file_size: int
    state: str
    progress: float
    error: str
    duration: float
    width: int
    height: int
    frames_processed: int
    events_created: int
    stream_settings: dict
    created_by: int | None
    created_date: datetime
    started_date: datetime | None
    finished_date: datetime | None


def _get_video(db: DbSession, video_id: int) -> models.VideoArchive:
    video = db.get(models.VideoArchive, video_id)
    if video is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Видеофайл не найден")
    return video


def _parse_settings(raw: str | None) -> dict:
    """stream_settings приходит строкой JSON в составе формы."""
    if not raw:
        return {}
    try:
        return StreamSettings(**json.loads(raw)).model_dump(exclude_defaults=True)
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Некорректные параметры обработки: {exc}")


@router.get("/videos/", response_model=Page[VideoOut])
def list_videos(
    camera_group: int | None = None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("videoarchive:view")),
):
    V = models.VideoArchive
    conds = []
    if camera_group is not None:
        conds.append(V.camera_group_id == camera_group)
    if state:
        conds.append(V.state == state.upper())
    count = db.scalar(select(func.count()).select_from(V).where(*conds))
    rows = db.scalars(
        select(V).where(*conds).order_by(V.id.desc()).limit(max(1, min(limit, 500))).offset(max(0, offset))
    ).all()
    return {"count": count, "results": rows}


@router.post("/videos/", response_model=VideoOut, status_code=status.HTTP_201_CREATED)
def upload_video(
    request: Request,
    source_file: UploadFile = File(description="Видеофайл"),
    camera_group: int = Form(description="id группы камер"),
    name: str | None = Form(default=None, max_length=256, description="Название; по умолчанию — имя файла"),
    stream_settings: str | None = Form(default=None, description="Параметры обработки в виде JSON"),
    process: bool = Form(default=False, description="Сразу поставить в очередь на обработку"),
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("videoarchive:add")),
):
    """Загружает видеофайл. Обработка запускается параметром process или запросом /videos/{id}/process/."""
    group = _get_group(db, camera_group)
    settings = _parse_settings(stream_settings)
    original = PurePath(source_file.filename or "video").name
    ext = PurePath(original).suffix.lower()
    if ext not in config.VIDEO_EXTENSIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Неподдерживаемый тип файла «{ext}». Допустимы: {', '.join(sorted(config.VIDEO_EXTENSIONS))}",
        )

    # Имя в хранилище случайное: исходное имя файла в путь не попадает.
    file_name = secrets.token_hex(16) + ext
    path = storage.video_path(file_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with open(path, "wb") as out:
            while chunk := source_file.file.read(1 << 20):
                size += len(chunk)
                if size > config.MAX_VIDEO_BYTES:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"Файл больше {config.MAX_VIDEO_BYTES // (1024 * 1024)} МБ",
                    )
                out.write(chunk)
        info = probe_video(path)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    except VideoError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    video = models.VideoArchive(
        name=(name or PurePath(original).stem)[:256] or "Видео",
        camera_group_id=group.id,
        file_name=file_name,
        file_size=size,
        state="QUEUED" if process else "UPLOADED",
        duration=round(info.duration, 3),
        width=info.width,
        height=info.height,
        stream_settings=settings,
        created_by=actor.id,
    )
    db.add(video)
    db.flush()
    audit.log(db, request, actor, "create", "videoarchive", video.id, {"name": video.name, "size": size})
    db.commit()
    if process:
        video_worker.enqueue(video.id)
    return video


@router.get("/videos/{video_id}/", response_model=VideoOut)
def get_video(video_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("videoarchive:view"))):
    return _get_video(db, video_id)


@router.post("/videos/{video_id}/process/", response_model=VideoOut)
def process_video(
    video_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("videoarchive:change")),
):
    """Ставит файл в очередь на обработку. Повторная обработка заменяет прежние события этого файла."""
    video = _get_video(db, video_id)
    if video.state in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Файл уже в очереди или обрабатывается")
    video.state, video.cancel_requested, video.error, video.progress = "QUEUED", False, "", 0.0
    audit.log(db, request, actor, "process", "videoarchive", video.id)
    db.commit()
    video_worker.enqueue(video.id)
    return video


@router.post("/videos/{video_id}/stop/", response_model=VideoOut)
def stop_video(
    video_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("videoarchive:change")),
):
    """Останавливает обработку. Уже созданные события сохраняются."""
    video = _get_video(db, video_id)
    if video.state not in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Файл сейчас не обрабатывается")
    video.cancel_requested = True
    audit.log(db, request, actor, "stop", "videoarchive", video.id)
    db.commit()
    return video


@router.delete("/videos/{video_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_video(
    video_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("videoarchive:delete")),
):
    """Удаляет видеофайл вместе с созданными по нему событиями."""
    video = _get_video(db, video_id)
    if video.state in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала остановите обработку файла")
    removed = events.delete_video_results(db, video.id)
    file_name = video.file_name
    audit.log(db, request, actor, "delete", "videoarchive", video.id, {"name": video.name, "events": removed})
    db.delete(video)
    db.commit()
    storage.video_path(file_name).unlink(missing_ok=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""Видеоаналитика: счётчики лиц в кадре и пересечение линий."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session as DbSession

from .. import audit, models, storage
from ..db import get_db
from ..deps import require
from ..schemas import Page
from ..video.analytics import delete_counter_images
from ..video.streams import stream_manager

router = APIRouter(tags=["Видеоаналитика"])


def _naive(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        from datetime import timezone

        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _check_camera(db: DbSession, camera_id: int) -> None:
    camera = db.get(models.Camera, camera_id)
    if camera is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Камера не найдена")
    if camera.external_detector:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Аналитика работает только на камерах с видеопотоком")


def _fraction(value: float) -> float:
    if not 0 <= value <= 1:
        raise ValueError("Координаты задаются в долях кадра, от 0 до 1")
    return round(float(value), 4)


# --- Счётчики ---
class _CounterFields(BaseModel):
    @field_validator("roi", check_fields=False)
    @classmethod
    def _roi(cls, v):
        if not v:
            return [] if v is not None else None
        if len(v) != 4:
            raise ValueError("Область задаётся четырьмя числами: слева, сверху, справа, снизу")
        left, top, right, bottom = (_fraction(x) for x in v)
        if right - left < 0.02 or bottom - top < 0.02:
            raise ValueError("Область слишком мала")
        return [left, top, right, bottom]


class CounterCreate(_CounterFields):
    name: str = Field(min_length=1, max_length=256)
    camera_id: int
    comment: str = Field(default="", max_length=2048)
    active: bool = True
    count_interval: int = Field(default=10, ge=1, le=3600)
    roi: list[float] = []


class CounterUpdate(_CounterFields):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    camera_id: int | None = None
    comment: str | None = Field(default=None, max_length=2048)
    active: bool | None = None
    count_interval: int | None = Field(default=None, ge=1, le=3600)
    roi: list[float] | None = None


class CounterOut(BaseModel):
    id: int
    name: str
    camera_id: int
    comment: str
    active: bool
    count_interval: int
    roi: list[float]
    created_date: datetime
    modified_date: datetime
    last_count: int | None = None  # результат последнего подсчёта
    last_date: datetime | None = None


class CounterRecordOut(BaseModel):
    id: int
    counter_id: int
    created_date: datetime
    faces_count: int
    faces_bbox: list
    has_image: bool


class ChartPoint(BaseModel):
    date: datetime  # начало промежутка
    average: float
    maximum: int
    records: int


def _counter_out(db: DbSession, counter: models.Counter) -> CounterOut:
    R = models.CounterRecord
    last = db.execute(
        select(R.faces_count, R.created_date).where(R.counter_id == counter.id).order_by(R.id.desc()).limit(1)
    ).first()
    return CounterOut(
        id=counter.id, name=counter.name, camera_id=counter.camera_id, comment=counter.comment, active=counter.active,
        count_interval=counter.count_interval, roi=counter.roi or [], created_date=counter.created_date,
        modified_date=counter.modified_date, last_count=last[0] if last else None, last_date=last[1] if last else None,
    )


def _get_counter(db: DbSession, counter_id: int) -> models.Counter:
    counter = db.get(models.Counter, counter_id)
    if counter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Счётчик не найден")
    return counter


@router.get("/counters/", response_model=Page[CounterOut])
def list_counters(
    cameras: list[int] | None = Query(default=None),
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("counter:view")),
):
    query = select(models.Counter).order_by(models.Counter.id)
    if cameras:
        query = query.where(models.Counter.camera_id.in_(cameras))
    rows = db.scalars(query).all()
    return {"count": len(rows), "results": [_counter_out(db, c) for c in rows]}


@router.post("/counters/", response_model=CounterOut, status_code=status.HTTP_201_CREATED)
def create_counter(
    data: CounterCreate, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("counter:add")),
):
    _check_camera(db, data.camera_id)
    counter = models.Counter(**data.model_dump())
    db.add(counter)
    db.flush()
    audit.log(db, request, actor, "create", "counter", counter.id, {"name": counter.name})
    db.commit()
    stream_manager.refresh_analytics(counter.camera_id)
    return _counter_out(db, counter)


@router.get("/counters/{counter_id}/", response_model=CounterOut)
def get_counter(counter_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("counter:view"))):
    return _counter_out(db, _get_counter(db, counter_id))


@router.patch("/counters/{counter_id}/", response_model=CounterOut)
def update_counter(
    counter_id: int, data: CounterUpdate, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("counter:change")),
):
    counter = _get_counter(db, counter_id)
    fields = data.model_dump(exclude_none=True)
    old_camera = counter.camera_id
    if "camera_id" in fields:
        _check_camera(db, fields["camera_id"])
    for key, value in fields.items():
        setattr(counter, key, value)
    if fields:
        audit.log(db, request, actor, "update", "counter", counter.id, fields)
        db.commit()
        for camera_id in {old_camera, counter.camera_id}:
            stream_manager.refresh_analytics(camera_id)
    return _counter_out(db, counter)


@router.delete("/counters/{counter_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_counter(
    counter_id: int, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("counter:delete")),
):
    counter = _get_counter(db, counter_id)
    camera_id = counter.camera_id
    audit.log(db, request, actor, "delete", "counter", counter.id, {"name": counter.name})
    delete_counter_images(db, models.CounterRecord.counter_id == counter.id)
    db.delete(counter)
    db.commit()
    stream_manager.refresh_analytics(camera_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/counters/{counter_id}/records/")
def purge_counter_records(
    counter_id: int, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("counter:delete")),
):
    """Удаляет все накопленные записи счётчика."""
    counter = _get_counter(db, counter_id)
    R = models.CounterRecord
    delete_counter_images(db, R.counter_id == counter.id)
    removed = db.execute(delete(R).where(R.counter_id == counter.id)).rowcount
    audit.log(db, request, actor, "purge", "counter", counter.id, {"records": removed})
    db.commit()
    return {"deleted": removed}


def _record_conditions(counter, created_date_gte, created_date_lte, count_gte=None, count_lte=None) -> list:
    R = models.CounterRecord
    conds = []
    if counter is not None:
        conds.append(R.counter_id == counter)
    if created_date_gte:
        conds.append(R.created_date >= _naive(created_date_gte))
    if created_date_lte:
        conds.append(R.created_date <= _naive(created_date_lte))
    if count_gte is not None:
        conds.append(R.faces_count >= count_gte)
    if count_lte is not None:
        conds.append(R.faces_count <= count_lte)
    return conds


@router.get("/counter-records/", response_model=Page[CounterRecordOut])
def list_counter_records(
    counter: int | None = None,
    created_date_gte: datetime | None = None,
    created_date_lte: datetime | None = None,
    count_gte: int | None = Query(default=None, ge=0),
    count_lte: int | None = Query(default=None, ge=0),
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("counter:view")),
):
    R = models.CounterRecord
    conds = _record_conditions(counter, created_date_gte, created_date_lte, count_gte, count_lte)
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    count = db.scalar(select(func.count()).select_from(R).where(*conds))
    rows = db.scalars(select(R).where(*conds).order_by(R.id.desc()).limit(limit).offset(offset)).all()
    return {"count": count, "results": [CounterRecordOut.model_validate(r, from_attributes=True) for r in rows]}


@router.get("/counter-records/{record_id}/image/")
def counter_record_image(record_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("counter:view"))):
    path = storage.counter_image(record_id) if db.get(models.CounterRecord, record_id) is not None else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Снимок не сохранён")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/counters/{counter_id}/chart/", response_model=list[ChartPoint])
def counter_chart(
    counter_id: int,
    created_date_gte: datetime,
    created_date_lte: datetime | None = None,
    points: int = Query(default=60, ge=2, le=500, description="На сколько промежутков разбить период"),
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("counter:view")),
):
    """Данные для графика: среднее и наибольшее число лиц по равным промежуткам времени.
    Промежутки без подсчётов в ответ не попадают."""
    _get_counter(db, counter_id)
    start, end = _naive(created_date_gte), _naive(created_date_lte) or models.utcnow()
    if end <= start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Конец периода должен быть позже начала")
    step = (end - start).total_seconds() / points
    R = models.CounterRecord
    rows = db.execute(
        select(R.created_date, R.faces_count).where(R.counter_id == counter_id, R.created_date >= start, R.created_date <= end)
    ).all()
    buckets: dict[int, list[int]] = {}
    for created, faces in rows:
        buckets.setdefault(min(points - 1, int((created - start).total_seconds() / step)), []).append(faces)
    from datetime import timedelta

    return [
        ChartPoint(
            date=start + timedelta(seconds=index * step), average=round(sum(values) / len(values), 2),
            maximum=max(values), records=len(values),
        )
        for index, values in sorted(buckets.items())
    ]


# --- Линии ---
class _LineFields(BaseModel):
    @field_validator("points", check_fields=False)
    @classmethod
    def _points(cls, v):
        if v is None:
            return v
        if len(v) != 2 or any(len(p) != 2 for p in v):
            raise ValueError("Линия задаётся двумя точками: [[x1, y1], [x2, y2]]")
        points = [[_fraction(x), _fraction(y)] for x, y in v]
        if abs(points[0][0] - points[1][0]) + abs(points[0][1] - points[1][1]) < 0.02:
            raise ValueError("Точки линии слишком близко друг к другу")
        return points


class LineCreate(_LineFields):
    name: str = Field(min_length=1, max_length=256)
    camera_id: int
    comment: str = Field(default="", max_length=2048)
    active: bool = True
    points: list[list[float]]


class LineUpdate(_LineFields):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    camera_id: int | None = None
    comment: str | None = Field(default=None, max_length=2048)
    active: bool | None = None
    points: list[list[float]] | None = None


class LineOut(BaseModel):
    id: int
    name: str
    camera_id: int
    comment: str
    active: bool
    points: list[list[float]]
    created_date: datetime
    modified_date: datetime
    count_ab: int = 0  # пересечений из A в B за запрошенный период
    count_ba: int = 0


class LineCrossingOut(BaseModel):
    id: int
    line_id: int
    created_date: datetime
    direction: str
    face_event_id: int | None
    matched_card_id: int | None
    matched_card_name: str | None = None


def _line_out(db: DbSession, line: models.Line, since: datetime | None) -> LineOut:
    X = models.LineCrossing
    conds = [X.line_id == line.id] + ([X.created_date >= since] if since else [])
    ab, ba = db.execute(
        select(func.sum(case((X.direction == "AB", 1), else_=0)), func.sum(case((X.direction == "BA", 1), else_=0))).where(*conds)
    ).one()
    return LineOut(
        id=line.id, name=line.name, camera_id=line.camera_id, comment=line.comment, active=line.active,
        points=line.points, created_date=line.created_date, modified_date=line.modified_date,
        count_ab=ab or 0, count_ba=ba or 0,
    )


def _get_line(db: DbSession, line_id: int) -> models.Line:
    line = db.get(models.Line, line_id)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Линия не найдена")
    return line


@router.get("/lines/", response_model=Page[LineOut])
def list_lines(
    cameras: list[int] | None = Query(default=None),
    since: datetime | None = Query(default=None, description="С какого момента считать пересечения (по умолчанию — все)"),
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("line:view")),
):
    query = select(models.Line).order_by(models.Line.id)
    if cameras:
        query = query.where(models.Line.camera_id.in_(cameras))
    rows = db.scalars(query).all()
    return {"count": len(rows), "results": [_line_out(db, line, _naive(since)) for line in rows]}


@router.post("/lines/", response_model=LineOut, status_code=status.HTTP_201_CREATED)
def create_line(
    data: LineCreate, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("line:add")),
):
    _check_camera(db, data.camera_id)
    line = models.Line(**data.model_dump())
    db.add(line)
    db.flush()
    audit.log(db, request, actor, "create", "line", line.id, {"name": line.name})
    db.commit()
    stream_manager.refresh_analytics(line.camera_id)
    return _line_out(db, line, None)


@router.get("/lines/{line_id}/", response_model=LineOut)
def get_line(
    line_id: int, since: datetime | None = None, db: DbSession = Depends(get_db),
    _: models.User = Depends(require("line:view")),
):
    return _line_out(db, _get_line(db, line_id), _naive(since))


@router.patch("/lines/{line_id}/", response_model=LineOut)
def update_line(
    line_id: int, data: LineUpdate, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("line:change")),
):
    line = _get_line(db, line_id)
    fields = data.model_dump(exclude_none=True)
    old_camera = line.camera_id
    if "camera_id" in fields:
        _check_camera(db, fields["camera_id"])
    for key, value in fields.items():
        setattr(line, key, value)
    if fields:
        audit.log(db, request, actor, "update", "line", line.id, fields)
        db.commit()
        for camera_id in {old_camera, line.camera_id}:
            stream_manager.refresh_analytics(camera_id)
    return _line_out(db, line, None)


@router.delete("/lines/{line_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_line(
    line_id: int, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("line:delete")),
):
    line = _get_line(db, line_id)
    camera_id = line.camera_id
    audit.log(db, request, actor, "delete", "line", line.id, {"name": line.name})
    db.delete(line)
    db.commit()
    stream_manager.refresh_analytics(camera_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/line-crossings/", response_model=Page[LineCrossingOut])
def list_line_crossings(
    line: int | None = None,
    direction: str | None = Query(default=None, description="AB или BA"),
    matched_card: int | None = None,
    created_date_gte: datetime | None = None,
    created_date_lte: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("line:view")),
):
    X = models.LineCrossing
    conds = []
    if line is not None:
        conds.append(X.line_id == line)
    if direction in ("AB", "BA"):
        conds.append(X.direction == direction)
    if matched_card is not None:
        conds.append(X.matched_card_id == matched_card)
    if created_date_gte:
        conds.append(X.created_date >= _naive(created_date_gte))
    if created_date_lte:
        conds.append(X.created_date <= _naive(created_date_lte))
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    count = db.scalar(select(func.count()).select_from(X).where(*conds))
    rows = db.scalars(select(X).where(*conds).order_by(X.id.desc()).limit(limit).offset(offset)).all()
    card_ids = {r.matched_card_id for r in rows if r.matched_card_id}
    names = dict(db.execute(select(models.HumanCard.id, models.HumanCard.name).where(models.HumanCard.id.in_(card_ids))).all()) if card_ids else {}
    results = [
        LineCrossingOut(
            id=r.id, line_id=r.line_id, created_date=r.created_date, direction=r.direction, face_event_id=r.face_event_id,
            matched_card_id=r.matched_card_id, matched_card_name=names.get(r.matched_card_id),
        )
        for r in rows
    ]
    return {"count": count, "results": results}

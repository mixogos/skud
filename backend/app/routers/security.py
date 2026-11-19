"""Охранная аналитика: правила (вторжение, праздношатание, скопление, саботаж) и тревоги."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from .. import audit, models, storage
from ..db import get_db
from ..deps import require
from ..schemas import Page
from ..video.security import delete_security_events
from ..video.streams import stream_manager

router = APIRouter(tags=["Охранная аналитика"])

RULE_TYPES = {"intrusion", "loitering", "crowd", "tampering"}
ZONE_TYPES = {"intrusion", "loitering", "crowd"}  # нужна зона (кроме саботажа — весь кадр)


def _naive(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _check_camera(db: DbSession, camera_id: int) -> None:
    camera = db.get(models.Camera, camera_id)
    if camera is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Камера не найдена")
    if camera.external_detector:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Охранная аналитика работает только на камерах с видеопотоком")


def _clean_zone(zone) -> list:
    if not zone:
        return []
    if len(zone) < 3:
        raise ValueError("Зона задаётся многоугольником минимум из трёх точек")
    out = []
    for p in zone:
        if len(p) != 2:
            raise ValueError("Каждая точка зоны — пара чисел [x, y]")
        x, y = p
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("Координаты зоны задаются в долях кадра, от 0 до 1")
        out.append([round(float(x), 4), round(float(y), 4)])
    return out


def _clean_params(rule_type: str, params: dict) -> dict:
    params = dict(params or {})
    out: dict = {}
    if rule_type == "loitering":
        out["dwell_seconds"] = int(min(600, max(1, params.get("dwell_seconds", 10))))
    elif rule_type == "crowd":
        out["min_count"] = int(min(100, max(2, params.get("min_count", 3))))
        out["hold_seconds"] = int(min(60, max(0, params.get("hold_seconds", 3))))
    elif rule_type == "tampering":
        out["sensitivity"] = int(min(100, max(1, params.get("sensitivity", 50))))
    return out


# --- Правила ---
class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    camera_id: int
    rule_type: str
    comment: str = Field(default="", max_length=2048)
    active: bool = True
    zone: list = []
    params: dict = {}

    @field_validator("rule_type")
    @classmethod
    def _type(cls, v):
        if v not in RULE_TYPES:
            raise ValueError(f"Тип правила: {', '.join(sorted(RULE_TYPES))}")
        return v


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    comment: str | None = Field(default=None, max_length=2048)
    active: bool | None = None
    zone: list | None = None
    params: dict | None = None


class RuleOut(BaseModel):
    id: int
    name: str
    camera_id: int
    rule_type: str
    comment: str
    active: bool
    zone: list
    params: dict
    created_date: datetime
    modified_date: datetime


def _rule_out(rule: models.SecurityRule) -> RuleOut:
    return RuleOut(
        id=rule.id, name=rule.name, camera_id=rule.camera_id, rule_type=rule.rule_type, comment=rule.comment,
        active=rule.active, zone=rule.zone or [], params=rule.params or {},
        created_date=rule.created_date, modified_date=rule.modified_date,
    )


def _get_rule(db: DbSession, rule_id: int) -> models.SecurityRule:
    rule = db.get(models.SecurityRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Правило не найдено")
    return rule


@router.get("/security-rules/", response_model=Page[RuleOut])
def list_rules(
    cameras: list[int] | None = Query(default=None),
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("securityrule:view")),
):
    query = select(models.SecurityRule).order_by(models.SecurityRule.id)
    if cameras:
        query = query.where(models.SecurityRule.camera_id.in_(cameras))
    rows = db.scalars(query).all()
    return {"count": len(rows), "results": [_rule_out(r) for r in rows]}


@router.post("/security-rules/", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(
    data: RuleCreate, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("securityrule:add")),
):
    _check_camera(db, data.camera_id)
    try:
        zone = _clean_zone(data.zone)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if data.rule_type in ZONE_TYPES and not zone:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Для этого правила нужна зона на кадре камеры")
    rule = models.SecurityRule(
        name=data.name, camera_id=data.camera_id, rule_type=data.rule_type, comment=data.comment,
        active=data.active, zone=zone, params=_clean_params(data.rule_type, data.params),
    )
    db.add(rule)
    db.flush()
    audit.log(db, request, actor, "create", "securityrule", rule.id, {"name": rule.name, "type": rule.rule_type})
    db.commit()
    stream_manager.refresh_security(rule.camera_id)
    return _rule_out(rule)


@router.get("/security-rules/{rule_id}/", response_model=RuleOut)
def get_rule(rule_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("securityrule:view"))):
    return _rule_out(_get_rule(db, rule_id))


@router.patch("/security-rules/{rule_id}/", response_model=RuleOut)
def update_rule(
    rule_id: int, data: RuleUpdate, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("securityrule:change")),
):
    rule = _get_rule(db, rule_id)
    fields = data.model_dump(exclude_none=True)
    try:
        if "zone" in fields:
            fields["zone"] = _clean_zone(fields["zone"])
            if rule.rule_type in ZONE_TYPES and not fields["zone"]:
                raise ValueError("Для этого правила нужна зона на кадре камеры")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if "params" in fields:
        fields["params"] = _clean_params(rule.rule_type, fields["params"])
    for key, value in fields.items():
        setattr(rule, key, value)
    if fields:
        audit.log(db, request, actor, "update", "securityrule", rule.id, fields)
        db.commit()
        stream_manager.refresh_security(rule.camera_id)
    return _rule_out(rule)


@router.delete("/security-rules/{rule_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: int, request: Request, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("securityrule:delete")),
):
    rule = _get_rule(db, rule_id)
    camera_id = rule.camera_id
    audit.log(db, request, actor, "delete", "securityrule", rule.id, {"name": rule.name})
    db.delete(rule)
    db.commit()
    stream_manager.refresh_security(camera_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Тревоги ---
class SecurityEventOut(BaseModel):
    id: int
    created_date: datetime
    camera_id: int | None
    rule_id: int | None
    rule_type: str
    rule_name: str
    severity: str
    message: str
    zone: list
    boxes: list
    frame_width: int
    frame_height: int
    has_image: bool
    acknowledged: bool


def _event_out(e: models.SecurityEvent) -> SecurityEventOut:
    return SecurityEventOut(
        id=e.id, created_date=e.created_date, camera_id=e.camera_id, rule_id=e.rule_id, rule_type=e.rule_type,
        rule_name=e.rule_name, severity=e.severity, message=e.message, zone=e.zone or [], boxes=e.boxes or [],
        frame_width=e.frame_width, frame_height=e.frame_height, has_image=e.has_image, acknowledged=e.acknowledged,
    )


@router.get("/events/security/", response_model=Page[SecurityEventOut])
def list_security_events(
    cameras: list[int] | None = Query(default=None),
    rule_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    acknowledged: bool | None = Query(default=None),
    created_date_gte: datetime | None = None,
    created_date_lte: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("securityevent:view")),
):
    E = models.SecurityEvent
    conds = []
    if cameras:
        conds.append(E.camera_id.in_(cameras))
    if rule_type in RULE_TYPES:
        conds.append(E.rule_type == rule_type)
    if severity in ("alarm", "warning"):
        conds.append(E.severity == severity)
    if acknowledged is not None:
        conds.append(E.acknowledged.is_(acknowledged))
    if created_date_gte:
        conds.append(E.created_date >= _naive(created_date_gte))
    if created_date_lte:
        conds.append(E.created_date <= _naive(created_date_lte))
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    count = db.scalar(select(func.count()).select_from(E).where(*conds))
    rows = db.scalars(select(E).where(*conds).order_by(E.id.desc()).limit(limit).offset(offset)).all()
    return {"count": count, "results": [_event_out(e) for e in rows]}


@router.get("/events/security/{event_id}/image/")
def security_event_image(
    event_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("securityevent:view"))
):
    path = storage.security_image(event_id) if db.get(models.SecurityEvent, event_id) is not None else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Снимок не сохранён")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/events/security/{event_id}/acknowledge/", response_model=SecurityEventOut)
def acknowledge_event(
    event_id: int, db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("securityevent:change")),
):
    event = db.get(models.SecurityEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Событие не найдено")
    if not event.acknowledged:
        event.acknowledged, event.acknowledged_by, event.acknowledged_date = True, actor.id, models.utcnow()
        db.commit()
    return _event_out(event)


@router.delete("/events/security/")
def purge_security_events(
    cameras: list[int] | None = Query(default=None),
    acknowledged: bool | None = Query(default=None),
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("securityevent:delete")),
):
    """Удаляет охранные события (по фильтру камер/статуса) вместе со снимками."""
    E = models.SecurityEvent
    conds = []
    if cameras:
        conds.append(E.camera_id.in_(cameras))
    if acknowledged is not None:
        conds.append(E.acknowledged.is_(acknowledged))
    removed = delete_security_events(db, *conds)
    db.commit()
    return {"deleted": removed}

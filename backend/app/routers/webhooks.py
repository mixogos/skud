from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit, models, webhooks
from ..db import get_db
from ..deps import require
from ..schemas import Page

router = APIRouter(prefix="/webhooks", tags=["Вебхуки"])


RuleType = Literal["intrusion", "loitering", "crowd", "tampering"]
Severity = Literal["alarm", "warning"]


class SecurityFilters(BaseModel):
    """Условия для охранных тревог. Незаданное — не ограничивает."""

    rule_types: list[RuleType] = []
    severity: list[Severity] = []


class WebhookFilters(BaseModel):
    """Условия отправки. Незаданное условие не ограничивает."""

    # Какие виды событий отправлять: проходы/лица и/или охранные тревоги. Пусто — только проходы.
    events: list[Literal["face", "security"]] = []
    matched: bool | None = None
    watch_lists: list[int] = []
    camera_groups: list[int] = []
    cameras: list[int] = []
    access: Literal["granted", "denied"] | None = None
    security: SecurityFilters = SecurityFilters()


def _check_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Адрес должен начинаться с http:// или https://")
    if parts.username or parts.password:
        raise ValueError("Логин и пароль в адресе не допускаются — используйте секрет для подписи")
    return value


class WebhookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    url: str = Field(max_length=2048)
    active: bool = True
    filters: WebhookFilters = WebhookFilters()
    secret: str = Field(default="", max_length=256)
    send_attempts: int = Field(default=3, ge=1, le=10)

    _url = field_validator("url")(_check_url)


class WebhookUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    url: str | None = Field(default=None, max_length=2048)
    active: bool | None = None
    filters: WebhookFilters | None = None
    secret: str | None = Field(default=None, max_length=256, description="Пустая строка убирает секрет")
    send_attempts: int | None = Field(default=None, ge=1, le=10)

    @field_validator("url")
    @classmethod
    def _url(cls, value):
        return value if value is None else _check_url(value)


class WebhookOut(BaseModel):
    id: int
    name: str
    url: str
    active: bool
    filters: WebhookFilters
    has_secret: bool
    send_attempts: int
    last_status: str
    last_error: str
    last_sent_date: datetime | None
    created_date: datetime
    modified_date: datetime


def _out(hook: models.Webhook) -> WebhookOut:
    return WebhookOut(
        id=hook.id, name=hook.name, url=hook.url, active=hook.active, filters=WebhookFilters(**(hook.filters or {})),
        has_secret=bool(hook.secret), send_attempts=hook.send_attempts, last_status=hook.last_status,
        last_error=hook.last_error, last_sent_date=hook.last_sent_date, created_date=hook.created_date,
        modified_date=hook.modified_date,
    )


def _get(db: DbSession, hook_id: int) -> models.Webhook:
    hook = db.get(models.Webhook, hook_id)
    if hook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Вебхук не найден")
    return hook


@router.get("/", response_model=Page[WebhookOut])
def list_webhooks(db: DbSession = Depends(get_db), _: models.User = Depends(require("webhook:view"))):
    rows = db.scalars(select(models.Webhook).order_by(models.Webhook.id)).all()
    return {"count": len(rows), "results": [_out(h) for h in rows]}


@router.post("/", response_model=WebhookOut, status_code=status.HTTP_201_CREATED)
def create_webhook(
    data: WebhookCreate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("webhook:add")),
):
    hook = models.Webhook(**{**data.model_dump(), "filters": data.filters.model_dump(exclude_defaults=True)})
    db.add(hook)
    db.flush()
    audit.log(db, request, actor, "create", "webhook", hook.id, {"name": hook.name, "url": hook.url})
    db.commit()
    return _out(hook)


@router.patch("/{hook_id}/", response_model=WebhookOut)
def update_webhook(
    hook_id: int,
    data: WebhookUpdate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("webhook:change")),
):
    hook = _get(db, hook_id)
    fields = data.model_dump(exclude_none=True)
    if data.filters is not None:
        fields["filters"] = data.filters.model_dump(exclude_defaults=True)
    for key, value in fields.items():
        setattr(hook, key, value)
    if fields:
        logged = {k: ("***" if k == "secret" else v) for k, v in fields.items()}
        audit.log(db, request, actor, "update", "webhook", hook.id, logged)
        db.commit()
    return _out(hook)


@router.post("/{hook_id}/test/")
def test_webhook(hook_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("webhook:change"))):
    """Отправляет пробное сообщение и сообщает, принял ли его адресат."""
    hook = _get(db, hook_id)
    sample = [{"id": 0, "test": True, "matched": False, "matched_lists": [], "camera_group_id": None, "access": None}]
    ok, error = webhooks.send_once(hook.url, hook.secret, "test", sample)
    hook.last_status, hook.last_error, hook.last_sent_date = "ok" if ok else "error", error, models.utcnow()
    db.commit()
    return {"ok": ok, "error": error}


@router.delete("/{hook_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    hook_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("webhook:delete")),
):
    hook = _get(db, hook_id)
    audit.log(db, request, actor, "delete", "webhook", hook.id, {"name": hook.name})
    db.delete(hook)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

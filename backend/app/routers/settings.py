from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session as DbSession

from .. import audit, models
from ..db import get_db
from ..deps import current_user, require
from ..settings_store import RuntimeSettings, get_settings, update_settings

router = APIRouter(prefix="/settings", tags=["Общие настройки"])


class SettingsUpdate(BaseModel):
    face_confidence_threshold: float | None = None
    unmatched_events_enabled: bool | None = None
    unmatched_acknowledge: bool | None = None
    liveness_threshold: float | None = None
    episode_event_timeout: int | None = None
    episode_max_duration: int | None = None


@router.get("/", response_model=RuntimeSettings)
def read_settings(db: DbSession = Depends(get_db), _: models.User = Depends(current_user)):
    return get_settings(db)


@router.patch("/", response_model=RuntimeSettings)
def change_settings(
    data: SettingsUpdate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("change_runtimesetting")),
):
    changes = data.model_dump(exclude_none=True)
    try:
        result = update_settings(db, changes)
    except ValidationError as exc:
        raise HTTPException(422, [f"{e['loc'][0]}: {e['msg']}" for e in exc.errors()])
    if changes:
        audit.log(db, request, actor, "update", "runtimesetting", "", changes)
        db.commit()
    return result

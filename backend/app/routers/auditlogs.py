from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import models, schemas
from ..db import get_db
from ..deps import require

router = APIRouter(prefix="/auditlogs", tags=["Журнал действий"])


@router.get("/", response_model=schemas.Page[schemas.AuditLogOut])
def list_audit_logs(
    user_id: int | None = None,
    user_name: str | None = None,
    action: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    created_date_gte: datetime | None = None,
    created_date_lte: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("view_auditlog")),
):
    A = models.AuditLog
    conds = []
    if user_id is not None:
        conds.append(A.user_id == user_id)
    if user_name:
        conds.append(A.user_name == user_name)
    if action:
        conds.append(A.action == action)
    if object_type:
        conds.append(A.object_type == object_type)
    if object_id:
        conds.append(A.object_id == object_id)
    if created_date_gte:
        conds.append(A.created_date >= created_date_gte.replace(tzinfo=None))
    if created_date_lte:
        conds.append(A.created_date <= created_date_lte.replace(tzinfo=None))
    count = db.scalar(select(func.count()).select_from(A).where(*conds))
    rows = db.scalars(
        select(A).where(*conds).order_by(A.id.desc()).limit(max(1, min(limit, 500))).offset(max(0, offset))
    ).all()
    return {"count": count, "results": rows}

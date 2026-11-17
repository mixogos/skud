from fastapi import Request
from sqlalchemy.orm import Session as DbSession

from . import models


def log(
    db: DbSession,
    request: Request | None,
    user: models.User | None,
    action: str,
    object_type: str = "",
    object_id: int | str = "",
    details: dict | None = None,
    user_name: str | None = None,
) -> None:
    """Добавляет запись в журнал действий. Фиксируется вместе с текущей транзакцией."""
    db.add(
        models.AuditLog(
            user_id=user.id if user else None,
            user_name=user_name if user_name is not None else (user.name if user else ""),
            action=action,
            object_type=object_type,
            object_id=str(object_id),
            details=details or {},
            ip=request.client.host if request and request.client else "",
        )
    )

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import audit, models, permissions, schemas
from ..db import get_db
from ..deps import current_user, require

# В FindFace Multi роли называются group — сохраняем тот же путь API.
router = APIRouter(tags=["Роли"])


def _get(db: DbSession, role_id: int) -> models.Role:
    role = db.get(models.Role, role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Роль не найдена")
    return role


def _check_name(db: DbSession, name: str, exclude_id: int | None = None) -> None:
    other = db.scalar(select(models.Role).where(models.Role.name == name))
    if other and other.id != exclude_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Роль с таким названием уже существует")


def _check_escalation(actor: models.User, perms: list[str]) -> None:
    """Нельзя выдать роли права, которых нет у самого пользователя."""
    extra = [p for p in perms if not actor.has_perm(p)]
    if extra:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Нельзя выдать права, которых нет у вас: {', '.join(extra)}")


@router.get("/permissions/")
def list_permissions(_: models.User = Depends(current_user)):
    """Справочник всех прав для формы редактирования роли."""
    return {
        "actions": permissions.ACTIONS,
        "entities": permissions.ENTITIES,
        "flags": permissions.FLAGS,
        "all": permissions.ALL_PERMISSIONS,
    }


@router.get("/groups/", response_model=schemas.Page[schemas.RoleOut])
def list_roles(db: DbSession = Depends(get_db), _: models.User = Depends(require("group:view"))):
    rows = db.scalars(select(models.Role).order_by(models.Role.id)).all()
    return {"count": len(rows), "results": rows}


@router.post("/groups/", response_model=schemas.RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(
    data: schemas.RoleCreate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("group:add")),
):
    _check_name(db, data.name)
    _check_escalation(actor, data.permissions)
    role = models.Role(name=data.name, permissions=data.permissions)
    db.add(role)
    db.flush()
    audit.log(db, request, actor, "create", "group", role.id, {"name": role.name})
    db.commit()
    return role


@router.get("/groups/{role_id}/", response_model=schemas.RoleOut)
def get_role(role_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("group:view"))):
    return _get(db, role_id)


@router.patch("/groups/{role_id}/", response_model=schemas.RoleOut)
def update_role(
    role_id: int,
    data: schemas.RoleUpdate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("group:change")),
):
    role = _get(db, role_id)
    details: dict = {}
    if data.name is not None and data.name != role.name:
        if role.predefined:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Предустановленную роль нельзя переименовать")
        _check_name(db, data.name, exclude_id=role.id)
        role.name = data.name
        details["name"] = data.name
    if data.permissions is not None:
        old, new = set(role.permissions or []), set(data.permissions)
        _check_escalation(actor, sorted(new - old))
        if old != new:
            role.permissions = data.permissions
            details["added"] = sorted(new - old)
            details["removed"] = sorted(old - new)
    if details:
        audit.log(db, request, actor, "update", "group", role.id, details)
        db.commit()
    return role


@router.delete("/groups/{role_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("group:delete")),
):
    role = _get(db, role_id)
    if role.predefined:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Предустановленную роль нельзя удалить")
    in_use = db.scalar(select(func.count()).select_from(models.User).where(models.User.role_id == role.id))
    if in_use:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Роль назначена пользователям ({in_use}), удаление невозможно")
    audit.log(db, request, actor, "delete", "group", role.id, {"name": role.name})
    db.delete(role)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

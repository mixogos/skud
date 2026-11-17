from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import audit, models, schemas, security
from ..db import get_db
from ..deps import require

router = APIRouter(prefix="/users", tags=["Пользователи"])


def _get(db: DbSession, user_id: int) -> models.User:
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    return user


def _check_role(db: DbSession, role_id: int) -> None:
    if db.get(models.Role, role_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Роль не найдена")


@router.get("/", response_model=schemas.Page[schemas.UserOut])
def list_users(
    limit: int = 50,
    offset: int = 0,
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("user:view")),
):
    count = db.scalar(select(func.count()).select_from(models.User))
    rows = db.scalars(
        select(models.User).order_by(models.User.id).limit(max(1, min(limit, 500))).offset(max(0, offset))
    ).all()
    return {"count": count, "results": rows}


@router.post("/", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    data: schemas.UserCreate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("user:add")),
):
    if db.scalar(select(models.User).where(models.User.name == data.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Пользователь с таким логином уже существует")
    _check_role(db, data.role_id)
    user = models.User(
        name=data.name,
        real_name=data.real_name,
        password_hash=security.hash_password(data.password),
        role_id=data.role_id,
        active=data.active,
    )
    db.add(user)
    db.flush()
    audit.log(db, request, actor, "create", "user", user.id, {"name": user.name, "role_id": user.role_id})
    db.commit()
    return user


@router.get("/{user_id}/", response_model=schemas.UserOut)
def get_user(user_id: int, db: DbSession = Depends(get_db), _: models.User = Depends(require("user:view"))):
    return _get(db, user_id)


@router.patch("/{user_id}/", response_model=schemas.UserOut)
def update_user(
    user_id: int,
    data: schemas.UserUpdate,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("user:change")),
):
    user = _get(db, user_id)
    fields = data.model_dump(exclude_unset=True, exclude_none=True)
    if user.is_superadmin:
        # Первого администратора нельзя лишить прав; менять его может только он сам.
        if actor.id != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Изменять первого администратора может только он сам")
        if fields.get("active") is False or ("role_id" in fields and fields["role_id"] != user.role_id):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Первого администратора нельзя отключить или сменить ему роль")
    if actor.id == user.id and fields.get("active") is False:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя отключить собственную учётную запись")
    if "role_id" in fields:
        _check_role(db, fields["role_id"])
        user.role_id = fields["role_id"]
    if "real_name" in fields:
        user.real_name = fields["real_name"]
    if "active" in fields:
        user.active = fields["active"]
    revoke = fields.get("active") is False
    if "password" in fields:
        user.password_hash = security.hash_password(fields.pop("password"))
        fields["password"] = "***"
        revoke = True
    if revoke:
        # Отключение и сброс пароля закрывают все сессии пользователя.
        for s in list(user.sessions):
            if not (actor.id == user.id):
                db.delete(s)
    if fields:
        audit.log(db, request, actor, "update", "user", user.id, fields)
        db.commit()
    return user


@router.delete("/{user_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    request: Request,
    db: DbSession = Depends(get_db),
    actor: models.User = Depends(require("user:delete")),
):
    user = _get(db, user_id)
    if user.is_superadmin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Первого администратора нельзя удалить")
    if user.id == actor.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя удалить собственную учётную запись")
    audit.log(db, request, actor, "delete", "user", user.id, {"name": user.name})
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

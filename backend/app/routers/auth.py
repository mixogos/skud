from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit, config, models, schemas, security
from ..db import get_db
from ..deps import client_ip, current_session, current_user

router = APIRouter(tags=["Вход и сессии"])

# Хеш для сравнения, когда пользователя нет: время ответа не выдаёт существование логина.
_DUMMY_HASH = security.hash_password("dummy-password")


def me_out(user: models.User) -> schemas.MeOut:
    base = schemas.UserOut.model_validate(user).model_dump()
    return schemas.MeOut(**base, role_name=user.role.name, permissions=sorted(user.role.permissions or []))


@router.post("/auth/login/", response_model=schemas.LoginOut)
def login(data: schemas.LoginIn, request: Request, db: DbSession = Depends(get_db)):
    user = db.scalar(select(models.User).where(models.User.name == data.name))
    now = models.utcnow()

    if user and user.locked_until and user.locked_until > now:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много неудачных попыток входа. Повторите позже.",
        )

    ok = security.verify_password(data.password, user.password_hash if user else _DUMMY_HASH)
    if not (user and ok and user.active):
        if user and not ok:
            user.failed_logins += 1
            if user.failed_logins >= config.MAX_FAILED_LOGINS:
                user.locked_until = now + timedelta(minutes=config.LOCK_MINUTES)
                user.failed_logins = 0
        audit.log(db, request, user, "login_failed", "user", user.id if user else "", user_name=data.name[:128])
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")

    user.failed_logins = 0
    user.locked_until = None
    token = security.new_token()
    session = models.Session(
        user_id=user.id,
        token_hash=security.token_hash(token),
        expires=now + timedelta(hours=config.SESSION_TTL_HOURS),
        ip=client_ip(request),
        user_agent=request.headers.get("User-Agent", "")[:512],
    )
    db.add(session)
    audit.log(db, request, user, "login", "user", user.id)
    db.commit()
    return schemas.LoginOut(token=token, expires=session.expires, user=me_out(user))


@router.post("/auth/logout/", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, session: models.Session = Depends(current_session), db: DbSession = Depends(get_db)):
    audit.log(db, request, session.user, "logout", "user", session.user_id)
    db.delete(session)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/", response_model=schemas.Page[schemas.SessionOut])
def list_sessions(
    all_users: bool = False,
    session: models.Session = Depends(current_session),
    db: DbSession = Depends(get_db),
):
    """Сессии текущего пользователя. С all_users=true и правом user:view — сессии всех пользователей."""
    user = session.user
    query = select(models.Session).where(models.Session.expires > models.utcnow())
    if all_users:
        if not user.has_perm("user:view"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав: user:view")
    elif user.has_perm("all_own_sessions:view"):
        query = query.where(models.Session.user_id == user.id)
    else:
        query = query.where(models.Session.id == session.id)
    rows = db.scalars(query.order_by(models.Session.last_used.desc())).all()
    results = []
    for row in rows:
        item = schemas.SessionOut.model_validate(row)
        item.current = row.id == session.id
        results.append(item)
    return {"count": len(results), "results": results}


@router.delete("/sessions/{session_id}/", status_code=status.HTTP_204_NO_CONTENT)
def close_session(
    session_id: int,
    request: Request,
    session: models.Session = Depends(current_session),
    db: DbSession = Depends(get_db),
):
    user = session.user
    target = db.get(models.Session, session_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    if target.user_id == user.id:
        allowed = target.id == session.id or user.has_perm("all_own_sessions:delete")
    else:
        allowed = user.has_perm("user:change")
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав для закрытия этой сессии")
    audit.log(db, request, user, "delete", "session", target.id, {"user_id": target.user_id})
    db.delete(target)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users/me/", response_model=schemas.MeOut)
def get_me(user: models.User = Depends(current_user)):
    return me_out(user)


@router.patch("/users/me/", response_model=schemas.MeOut)
def update_me(
    data: schemas.MeUpdate,
    request: Request,
    session: models.Session = Depends(current_session),
    db: DbSession = Depends(get_db),
):
    user = session.user
    changed = []
    if data.real_name is not None:
        user.real_name = data.real_name
        changed.append("real_name")
    if data.new_password is not None:
        if not data.old_password or not security.verify_password(data.old_password, user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный текущий пароль")
        user.password_hash = security.hash_password(data.new_password)
        # Смена пароля закрывает все остальные сессии.
        for other in list(user.sessions):
            if other.id != session.id:
                db.delete(other)
        changed.append("password")
    if changed:
        audit.log(db, request, user, "update", "user", user.id, {"fields": changed})
        db.commit()
    return me_out(user)

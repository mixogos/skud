from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import config, models, permissions, security
from .db import get_db

LOCAL_SESSION_MARK = "local-no-auth"  # вместо хеша токена у сессии режима без входа


def auth_bypassed(host: str | None) -> bool:
    """Режим без входа действует только для запросов с этого же компьютера."""
    return config.AUTH_DISABLED and host in config.LOCAL_HOSTS


def local_session(db: DbSession) -> models.Session:
    """Сессия администратора для режима без входа; при необходимости создаёт и его самого."""
    now = models.utcnow()
    session = db.scalar(select(models.Session).where(models.Session.token_hash == LOCAL_SESSION_MARK))
    if session is not None and session.user.active:
        if session.expires - now < timedelta(days=1):
            session.expires = now + timedelta(days=30)
            db.commit()
        return session
    admin = db.scalar(select(models.User).where(models.User.is_superadmin))
    if admin is None:
        role = db.scalar(select(models.Role).where(models.Role.name == permissions.ROLE_ADMIN))
        # Пароль случайный и нигде не показывается: задать свой — python -m app.cli reset-password admin
        admin = models.User(
            name="admin", real_name="Администратор", role_id=role.id, is_superadmin=True,
            password_hash=security.hash_password(security.new_token()),
        )
        db.add(admin)
        db.flush()
    if session is None:
        session = models.Session(user_id=admin.id, token_hash=LOCAL_SESSION_MARK, user_agent="Режим без входа", expires=now)
        db.add(session)
    session.user_id, session.expires = admin.id, now + timedelta(days=30)
    db.commit()
    db.refresh(session)
    return session


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def current_session(request: Request, db: DbSession = Depends(get_db)) -> models.Session:
    """Проверяет заголовок «Authorization: Token <токен>» и возвращает сессию."""
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Требуется авторизация",
        headers={"WWW-Authenticate": "Token"},
    )
    bypass = auth_bypassed(client_ip(request))
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "token" or not token.strip():
        if bypass:
            return local_session(db)
        raise unauthorized
    session = db.scalar(
        select(models.Session).where(models.Session.token_hash == security.token_hash(token.strip()))
    )
    now = models.utcnow()
    if session is None or session.expires < now or not session.user.active:
        if bypass:
            return local_session(db)
        raise unauthorized
    # Продлеваем сессию, но не пишем в базу на каждый запрос.
    if now - session.last_used > timedelta(minutes=1):
        session.last_used = now
        session.expires = now + timedelta(hours=config.SESSION_TTL_HOURS)
        db.commit()
    return session


def current_user(session: models.Session = Depends(current_session)) -> models.User:
    return session.user


def require(*perms: str):
    """Зависимость: пользователь должен иметь все перечисленные права."""

    def checker(user: models.User = Depends(current_user)) -> models.User:
        missing = [p for p in perms if not user.has_perm(p)]
        if missing:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Недостаточно прав: {', '.join(missing)}")
        return user

    return checker

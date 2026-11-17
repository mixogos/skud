"""Создание таблиц, предустановленных ролей и первого администратора."""
import json

from sqlalchemy import inspect, text
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import models, permissions, security
from .db import Base, SessionLocal, engine


def add_missing_columns() -> list[str]:
    """Добавляет в существующие таблицы колонки, появившиеся в новых версиях программы.

    Покрывает только добавление колонок — этого достаточно, пока схема лишь расширяется.
    """
    added = []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
                default = column.default
                if default is not None:
                    value = default.arg(None) if default.is_callable else default.arg
                    if isinstance(value, bool):
                        literal = "1" if value else "0"
                    elif isinstance(value, (int, float)):
                        literal = repr(value)
                    elif isinstance(value, (dict, list)):
                        literal = "'" + json.dumps(value) + "'"
                    else:
                        literal = "'" + str(value).replace("'", "''") + "'"
                    ddl += f" DEFAULT {literal}"
                    if not column.nullable:
                        ddl += " NOT NULL"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    return added


# Сущности, появившиеся после первых версий: (таблица-признак, сущности прав).
_NEW_ENTITIES = (("counters", ("counter", "line")),)


def init_db() -> None:
    tables_before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    add_missing_columns()
    with SessionLocal() as db:
        existing = {r.name: r for r in db.scalars(select(models.Role))}
        for name, perms in permissions.default_roles().items():
            if name not in existing:
                db.add(models.Role(name=name, permissions=perms, predefined=True))
                continue
            # Обновление программы: предустановленные роли получают права на новые разделы (один раз).
            for table, entities in _NEW_ENTITIES:
                if tables_before and table not in tables_before:
                    fresh = [p for p in perms if p.split(":")[0] in entities]
                    existing[name].permissions = sorted(set(existing[name].permissions or []) | set(fresh))
        if not db.scalar(select(models.WatchList).where(models.WatchList.predefined)):
            db.add(models.WatchList(name="Список по умолчанию", predefined=True))
        if not db.scalar(select(models.CameraGroup).where(models.CameraGroup.predefined)):
            db.add(models.CameraGroup(name="Группа по умолчанию", predefined=True))
        db.commit()


def create_superadmin(db: DbSession, name: str, password: str) -> models.User:
    """Создаёт первого администратора. Повторный вызов запрещён."""
    if db.scalar(select(models.User).where(models.User.is_superadmin)):
        raise ValueError("Первый администратор уже существует")
    role = db.scalar(select(models.Role).where(models.Role.name == permissions.ROLE_ADMIN))
    user = models.User(
        name=name,
        real_name="Администратор",
        password_hash=security.hash_password(password),
        role_id=role.id,
        is_superadmin=True,
    )
    db.add(user)
    db.commit()
    return user

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config
from .permissions import ALL_PERMISSIONS

T = TypeVar("T")

Password = Field(min_length=config.MIN_PASSWORD_LENGTH, max_length=256)


class Page(BaseModel, Generic[T]):
    count: int
    results: list[T]


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Роли ---
class _PermsMixin(BaseModel):
    @field_validator("permissions", check_fields=False)
    @classmethod
    def _known(cls, v):
        if v is None:
            return v
        unknown = sorted(set(v) - set(ALL_PERMISSIONS))
        if unknown:
            raise ValueError(f"Неизвестные права: {', '.join(unknown)}")
        return sorted(set(v))


class RoleCreate(_PermsMixin):
    name: str = Field(min_length=1, max_length=128)
    permissions: list[str] = []


class RoleUpdate(_PermsMixin):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    permissions: list[str] | None = None


class RoleOut(_Orm):
    id: int
    name: str
    permissions: list[str]
    predefined: bool
    created_date: datetime


# --- Пользователи ---
class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[\w.@+-]+$")
    real_name: str = Field(default="", max_length=256)
    password: str = Password
    role_id: int
    active: bool = True


class UserUpdate(BaseModel):
    real_name: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, min_length=config.MIN_PASSWORD_LENGTH, max_length=256)
    role_id: int | None = None
    active: bool | None = None


class MeUpdate(BaseModel):
    real_name: str | None = Field(default=None, max_length=256)
    old_password: str | None = None
    new_password: str | None = Field(default=None, min_length=config.MIN_PASSWORD_LENGTH, max_length=256)


class UserOut(_Orm):
    id: int
    name: str
    real_name: str
    active: bool
    is_superadmin: bool
    role_id: int
    created_date: datetime
    modified_date: datetime


class MeOut(UserOut):
    role_name: str
    permissions: list[str]


# --- Вход и сессии ---
class LoginIn(BaseModel):
    name: str
    password: str


class LoginOut(BaseModel):
    token: str
    expires: datetime
    user: MeOut


class SessionOut(_Orm):
    id: int
    user_id: int
    created_date: datetime
    last_used: datetime
    expires: datetime
    ip: str
    user_agent: str
    current: bool = False


# --- Журнал ---
class AuditLogOut(_Orm):
    id: int
    created_date: datetime
    user_id: int | None
    user_name: str
    action: str
    object_type: str
    object_id: str
    details: dict
    ip: str

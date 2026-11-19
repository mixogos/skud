"""Права доступа по образцу FindFace Multi: сущность + действие, плюс отдельные флаги.

Право записывается строкой «сущность:действие», например «humancard:view».
"""

ACTIONS = ("view", "add", "change", "delete")

# Сущности системы (названия совпадают с документацией FindFace Multi).
ENTITIES = {
    "user": "Пользователи",
    "group": "Роли",
    "humancard": "Карточки людей",
    "faceobject": "Фото лиц в карточках",
    "watchlist": "Списки наблюдения",
    "cameragroup": "Группы камер",
    "camera": "Камеры и внешние детекторы",
    "videoarchive": "Видеофайлы",
    "faceevent": "События распознавания лиц",
    "humanepisode": "Эпизоды с людьми",
    "counter": "Счётчики",
    "line": "Линии пересечения",
    "securityrule": "Правила охранной аналитики",
    "securityevent": "Охранные события",
    "webhook": "Вебхуки",
    "report": "Отчёты",
    "all_own_sessions": "Все свои сессии на разных устройствах",
}

# Права-флаги на отдельные функции.
FLAGS = {
    "view_auditlog": "Просмотр журнала действий",
    "change_runtimesetting": "Изменение общих настроек",
    "batchupload_cards": "Пакетная загрузка карточек",
}

ALL_PERMISSIONS = [f"{e}:{a}" for e in ENTITIES for a in ACTIONS] + list(FLAGS)

_ADMIN_ONLY_ENTITIES = {"user", "group"}
_ADMIN_ONLY_FLAGS = {"view_auditlog", "change_runtimesetting"}

ROLE_ADMIN = "Администратор"
ROLE_OPERATOR = "Оператор"
ROLE_USER = "Пользователь"


def default_roles() -> dict[str, list[str]]:
    """Предустановленные роли: Администратор, Оператор, Пользователь."""
    operator = [
        p
        for p in ALL_PERMISSIONS
        if p.split(":")[0] not in _ADMIN_ONLY_ENTITIES and p not in _ADMIN_ONLY_FLAGS
    ]
    # Пользователь: всё только на чтение, но с событиями и эпизодами можно работать.
    user = [
        f"{e}:view" for e in ENTITIES if e not in _ADMIN_ONLY_ENTITIES
    ] + ["faceevent:change", "humanepisode:change", "all_own_sessions:delete"]
    return {ROLE_ADMIN: list(ALL_PERMISSIONS), ROLE_OPERATOR: operator, ROLE_USER: user}

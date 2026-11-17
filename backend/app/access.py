"""Контроль доступа: решение «пропустить / отказать» по спискам наблюдения и расписанию."""
import re
from datetime import datetime, timezone

from . import matching, models

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
POLICIES = ("none", "allow", "deny")
GRANTED, DENIED = "granted", "denied"
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")


def validate_schedule(schedule: dict) -> dict:
    """Проверяет расписание вида {"monday": [["08:00", "20:00"]], ...}.

    Пустое расписание означает «без ограничений». Если расписание задано, в дни,
    которых в нём нет, прохода нет. Конец интервала может быть «24:00».
    """
    if not isinstance(schedule, dict):
        raise ValueError("Расписание должно быть объектом")
    result = {}
    for day, intervals in schedule.items():
        if day not in WEEKDAYS:
            raise ValueError(f"Неизвестный день недели: {day}")
        if not isinstance(intervals, list):
            raise ValueError(f"{day}: ожидается список интервалов")
        clean = []
        for interval in intervals:
            if not (isinstance(interval, (list, tuple)) and len(interval) == 2 and all(isinstance(v, str) and _TIME.match(v) for v in interval)):
                raise ValueError(f"{day}: интервал задаётся парой времён «ЧЧ:ММ»")
            start, end = interval
            if start >= end:
                raise ValueError(f"{day}: начало интервала должно быть раньше конца")
            clean.append([start, end])
        if clean:
            result[day] = clean
    return result


def to_local(when_utc: datetime) -> datetime:
    """Время события (UTC без пояса) в местном времени сервера."""
    return when_utc.replace(tzinfo=timezone.utc).astimezone()


def in_schedule(schedule: dict | None, local: datetime) -> bool:
    if not schedule:
        return True
    now = local.strftime("%H:%M")
    return any(start <= now < end for start, end in schedule.get(WEEKDAYS[local.weekday()], []))


def decide(match: matching.Match | None, when_utc: datetime) -> tuple[str, str]:
    """Возвращает (решение, причина).

    Запрещающий список сильнее разрешающего. Личное расписание карточки, если задано,
    заменяет расписания списков; иначе достаточно одного разрешающего списка, чьё
    расписание допускает проход в этот момент.
    """
    if match is None:
        return DENIED, "Человека нет в картотеке"
    for wl in match.watch_lists:
        if wl.access_policy == "deny":
            return DENIED, f"Список «{wl.name}» запрещает проход"
    allowing = [wl for wl in match.watch_lists if wl.access_policy == "allow"]
    if not allowing:
        return DENIED, "Нет списка, разрешающего проход"
    local = to_local(when_utc)
    if match.card.access_schedule:
        if in_schedule(match.card.access_schedule, local):
            return GRANTED, "Личное расписание"
        return DENIED, "Вне личного расписания"
    for wl in allowing:
        if in_schedule(wl.access_schedule, local):
            return GRANTED, f"Список «{wl.name}»"
    return DENIED, "Вне расписания"

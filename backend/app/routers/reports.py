"""Отчёты. Отчёт о рабочем времени строится по событиям с совпадением:
первое и последнее появление человека за каждый день."""
import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession

from .. import access, models
from ..db import get_db
from ..deps import require

router = APIRouter(prefix="/reports", tags=["Отчёты"])

MAX_DAYS = 92


def _local_day_start_utc(day: date) -> datetime:
    """Начало местных суток в UTC без пояса (как хранится время событий)."""
    return datetime.combine(day, time.min).astimezone().astimezone(timezone.utc).replace(tzinfo=None)


def _in_json_list(column, value: int):
    items = func.json_each(column).table_valued("value")
    return select(1).select_from(items).where(items.c.value == value).exists()


def _fmt_duration(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}"


@router.get("/worktime/")
def worktime_report(
    date_from: date = Query(description="Первый день периода"),
    date_to: date = Query(description="Последний день периода (включительно)"),
    watch_lists: list[int] | None = Query(default=None),
    camera_groups: list[int] | None = Query(default=None),
    card: int | None = None,
    format: str = Query(default="json", pattern="^(json|csv)$"),
    db: DbSession = Depends(get_db),
    _: models.User = Depends(require("report:view", "faceevent:view")),
):
    """Рабочее время по дням: приход — первое событие человека за день, уход — последнее.

    Дни считаются по местному времени сервера. format=csv возвращает файл для Excel.
    """
    if date_to < date_from:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Конец периода раньше начала")
    if (date_to - date_from).days >= MAX_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Период не должен превышать {MAX_DAYS} дня")

    E = models.FaceEvent
    conds = [
        E.matched_card_id.is_not(None),
        E.video_archive_id.is_(None),  # у событий из видеофайлов условное время
        E.created_date >= _local_day_start_utc(date_from),
        E.created_date < _local_day_start_utc(date_to + timedelta(days=1)),
    ]
    if watch_lists:
        conds.append(or_(*[_in_json_list(E.matched_lists, wl) for wl in watch_lists]))
    if camera_groups:
        conds.append(E.camera_group_id.in_(camera_groups))
    if card is not None:
        conds.append(E.matched_card_id == card)

    days: dict[tuple[int, date], list] = {}
    for card_id, created in db.execute(select(E.matched_card_id, E.created_date).where(*conds).order_by(E.created_date)):
        local = access.to_local(created)
        entry = days.setdefault((card_id, local.date()), [local, local, 0])
        entry[1] = local
        entry[2] += 1

    names = dict(
        db.execute(select(models.HumanCard.id, models.HumanCard.name).where(models.HumanCard.id.in_({k[0] for k in days}))).all()
    ) if days else {}
    rows = [
        {
            "card_id": card_id,
            "card_name": names.get(card_id, f"Карточка № {card_id}"),
            "date": day.isoformat(),
            "first_seen": first.strftime("%H:%M:%S"),
            "last_seen": last.strftime("%H:%M:%S"),
            "duration_seconds": int((last - first).total_seconds()),
            "events": count,
        }
        for (card_id, day), (first, last, count) in days.items()
    ]
    rows.sort(key=lambda r: (r["date"], r["card_name"].lower()))

    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(["Дата", "Сотрудник", "Приход", "Уход", "Время (ч:мм)", "Событий"])
        for r in rows:
            writer.writerow([r["date"], r["card_name"], r["first_seen"], r["last_seen"], _fmt_duration(r["duration_seconds"]), r["events"]])
        # BOM нужен, чтобы Excel правильно открыл кириллицу.
        return Response(
            content=("﻿" + buffer.getvalue()).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="worktime_{date_from}_{date_to}.csv"'},
        )
    return {"count": len(rows), "results": rows}

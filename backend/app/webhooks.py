"""Вебхуки: отправка новых событий во внешние системы (например, контроллеру двери)."""
import hashlib
import hmac
import json
import logging
import queue
import threading
import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import config, models
from .db import SessionLocal

log = logging.getLogger("skud.webhooks")


def _event_kinds(filters: dict | None) -> set:
    """Какие виды событий получает вебхук. Пусто — только проходы/лица (для старых вебхуков)."""
    kinds = (filters or {}).get("events")
    return set(kinds) if kinds else {"face"}


def matches(filters: dict | None, event: dict) -> bool:
    """Подходит ли событие прохода под условия вебхука. Пустые условия — подходит любое событие."""
    f = filters or {}
    if "face" not in _event_kinds(f):
        return False
    if f.get("matched") is not None and event["matched"] != f["matched"]:
        return False
    if f.get("watch_lists") and not set(f["watch_lists"]) & set(event["matched_lists"]):
        return False
    if f.get("camera_groups") and event["camera_group_id"] not in f["camera_groups"]:
        return False
    if f.get("cameras") and event["camera_id"] not in f["cameras"]:
        return False
    if f.get("access") and event.get("access") != f["access"]:
        return False
    return True


def matches_security(filters: dict | None, event: dict) -> bool:
    """Подходит ли охранная тревога под условия вебхука."""
    f = filters or {}
    if "security" not in _event_kinds(f):
        return False
    if f.get("cameras") and event.get("camera_id") not in f["cameras"]:
        return False
    sec = f.get("security") or {}
    if sec.get("rule_types") and event["rule_type"] not in sec["rule_types"]:
        return False
    if sec.get("severity") and event["severity"] not in sec["severity"]:
        return False
    return True


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send_once(url: str, secret: str, event_type: str, payload) -> tuple[bool, str]:
    """Одна попытка отправки. Возвращает (успех, описание ошибки)."""
    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "skud-webhook", "X-Skud-Event": event_type}
    if secret:
        headers["X-Skud-Signature"] = sign(secret, body)
    try:
        response = httpx.post(url, content=body, headers=headers, timeout=config.WEBHOOK_TIMEOUT, follow_redirects=False)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:512]
    if 200 <= response.status_code < 300:
        return True, ""
    return False, f"Ответ сервера: {response.status_code}"


class WebhookSender:
    """Очередь отправки: один фоновый поток, повторы с нарастающей паузой."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="webhook-sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=10)
        self._thread = None

    def enqueue(self, webhook_id: int, event_type: str, payload) -> None:
        self._queue.put((webhook_id, event_type, payload))

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Ждёт, пока очередь опустеет (для тестов)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._deliver(*item)
            except Exception:
                log.exception("Сбой отправки вебхука")
            finally:
                self._queue.task_done()

    def _deliver(self, webhook_id: int, event_type: str, payload) -> None:
        with SessionLocal() as db:
            hook = db.get(models.Webhook, webhook_id)
            if hook is None or not hook.active:
                return
            url, secret, attempts = hook.url, hook.secret, max(1, hook.send_attempts)
        ok, error = False, ""
        for attempt in range(attempts):
            if attempt:
                time.sleep(config.WEBHOOK_RETRY_DELAY * 2 ** (attempt - 1))
            ok, error = send_once(url, secret, event_type, payload)
            if ok:
                break
        with SessionLocal() as db:
            hook = db.get(models.Webhook, webhook_id)
            if hook is not None:
                hook.last_status, hook.last_error = ("ok", "") if ok else ("error", error)
                hook.last_sent_date = models.utcnow()
                db.commit()


sender = WebhookSender()


def dispatch(db: DbSession, event_type: str, event: dict) -> int:
    """Ставит событие прохода в очередь отправки всем подходящим активным вебхукам."""
    count = 0
    for hook in db.scalars(select(models.Webhook).where(models.Webhook.active)):
        if matches(hook.filters, event):
            sender.enqueue(hook.id, event_type, [event])
            count += 1
    return count


def dispatch_security(db: DbSession, event: dict) -> int:
    """Ставит охранную тревогу в очередь отправки всем подходящим активным вебхукам."""
    count = 0
    for hook in db.scalars(select(models.Webhook).where(models.Webhook.active)):
        if matches_security(hook.filters, event):
            sender.enqueue(hook.id, "security_alarm", [event])
            count += 1
    return count

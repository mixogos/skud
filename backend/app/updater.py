"""Автообновление через GitHub Releases.

Приложение сравнивает свою версию с последним релизом в заданном репозитории
(`config.UPDATE_REPO` вида «владелец/репозиторий») и, если там версия новее,
скачивает приложенный к релизу установщик `seture.exe` и запускает его.
Установщик закрывает работающую программу, обновляет её и запускает снова;
база данных и настройки сохраняются (лежат отдельно, в папке пользователя).
"""
import os
import re
import subprocess
import tempfile
import threading
import time

import httpx

from . import config
from .version import __version__

CACHE_TTL = 1800  # как часто перепроверять релизы, секунд
_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _version_tuple(value: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", value or "")
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def _fetch() -> dict:
    repo = config.UPDATE_REPO
    if not repo:
        return {"configured": False, "current": __version__, "update_available": False}
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "SKUD-Updater"}
    if config.UPDATE_TOKEN:
        headers["Authorization"] = f"Bearer {config.UPDATE_TOKEN}"
    resp = httpx.get(url, headers=headers, timeout=10, follow_redirects=True)
    if resp.status_code == 404:
        return {"configured": True, "current": __version__, "latest": None,
                "update_available": False, "note": "релизов в репозитории пока нет"}
    resp.raise_for_status()
    data = resp.json()
    tag = data.get("tag_name", "") or ""
    asset = next((a for a in data.get("assets", []) if a.get("name", "").lower().endswith(".exe")), None)
    newer = _version_tuple(tag) > _version_tuple(__version__)
    return {
        "configured": True,
        "current": __version__,
        "latest": tag.lstrip("v") or None,
        "update_available": bool(newer and asset),
        "download_url": asset.get("browser_download_url") if asset else None,
        "size": asset.get("size") if asset else None,
        "notes": (data.get("body") or "")[:4000],
        "published": data.get("published_at"),
        "html_url": data.get("html_url"),
    }


def status(force: bool = False) -> dict:
    """Состояние обновлений (кэшируется, чтобы не дёргать GitHub на каждый запрос)."""
    with _lock:
        if not force and _cache["data"] is not None and time.time() - _cache["at"] < CACHE_TTL:
            return _cache["data"]
    try:
        data = _fetch()
    except Exception as exc:  # сеть недоступна или GitHub ответил ошибкой — не падаем
        data = {"configured": bool(config.UPDATE_REPO), "current": __version__,
                "update_available": False, "error": f"Не удалось проверить обновления: {exc}"}
    with _lock:
        _cache.update(at=time.time(), data=data)
    return data


def apply() -> dict:
    """Скачивает установщик последней версии и запускает его. Программа закроется и обновится."""
    data = status(force=True)
    if not data.get("update_available") or not data.get("download_url"):
        return {"started": False, "message": "Обновление недоступно"}
    target = os.path.join(tempfile.gettempdir(), "SKUD-setup-update.exe")
    headers = {"User-Agent": "SKUD-Updater"}
    if config.UPDATE_TOKEN:
        headers["Authorization"] = f"Bearer {config.UPDATE_TOKEN}"
    with httpx.stream("GET", data["download_url"], timeout=None, follow_redirects=True, headers=headers) as resp:
        resp.raise_for_status()
        with open(target, "wb") as out:
            for chunk in resp.iter_bytes(1 << 20):
                out.write(chunk)
    # Небольшая задержка, чтобы этот ответ успел дойти до интерфейса; затем установщик сам
    # закроет все экземпляры СКУД на этом ПК, обновит программу и запустит её снова.
    quoted = target.replace('"', '')
    subprocess.Popen(
        f'ping 127.0.0.1 -n 3 >nul & "{quoted}" /SILENT',
        shell=True, creationflags=0x08000000,  # CREATE_NO_WINDOW — без мелькания консоли
    )
    return {"started": True, "version": data.get("latest")}

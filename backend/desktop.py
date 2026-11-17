"""Запуск СКУД как настольного приложения: своё окно, без браузера и без консоли.

Внутри работает тот же сервер (FastAPI), но пользователь видит обычное окно Windows.
Веб-движок — встроенный в систему WebView2 (на Windows 11 есть по умолчанию).

Запуск при разработке:   python desktop.py
Только сервер (для проверки, без окна):  python desktop.py --server-only
"""
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP_TITLE = "СКУД — распознавание лиц"
PREFERRED_PORT = 8010


def resource_base() -> Path:
    """Папка с ресурсами: в собранном приложении — рядом с .exe, при разработке — папка backend."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """Папка для изменяемых данных (база, загрузки) — у каждого пользователя своя, доступная на запись."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = root / "SKUD" / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_environment() -> None:
    """Задаёт пути и режим работы до импорта приложения. Переменные окружения имеют приоритет."""
    base = resource_base()
    if getattr(sys, "frozen", False):
        models, frontend = base / "models", base / "frontend"
    else:
        models, frontend = base / "data" / "models", base.parent / "frontend" / "dist"
    os.environ.setdefault("SKUD_DATA_DIR", str(user_data_dir()))
    os.environ.setdefault("SKUD_MODELS_DIR", str(models))
    os.environ.setdefault("SKUD_FRONTEND_DIR", str(frontend))
    # Настольное приложение открывается сразу, без окна входа (доступ только с этого компьютера).
    os.environ.setdefault("SKUD_NO_AUTH", "1")
    # Видеофайл в роли камеры разрешаем — удобно для проверки без настоящих камер.
    os.environ.setdefault("SKUD_CAMERA_FILE_SOURCES", "1")


def pick_port() -> int:
    """Пытается занять привычный порт, иначе берёт любой свободный."""
    for candidate in (PREFERRED_PORT, 0):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", candidate))
            port = sock.getsockname()[1]
            sock.close()
            return port
        except OSError:
            sock.close()
    raise RuntimeError("Не удалось найти свободный порт")


def start_server(port: int):
    """Запускает сервер в фоновом потоке. Возвращает объект сервера, чтобы потом его остановить."""
    import uvicorn

    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # обработчики сигналов ставятся только в главном потоке
    threading.Thread(target=server.run, name="skud-server", daemon=True).start()
    return server


def wait_ready(port: int, timeout: float = 90.0) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def set_app_mutex() -> None:
    """Именованная метка работающего приложения — по ней установщик закрывает его при обновлении."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\SKUD_App_Mutex")
    except Exception:
        pass


def main() -> int:
    set_app_mutex()
    configure_environment()
    port = pick_port()
    server = start_server(port)
    ready = wait_ready(port)

    if "--server-only" in sys.argv:  # режим проверки без окна
        print(f"SERVER http://127.0.0.1:{port}  READY={ready}", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True
            return 0

    import webview

    if not ready:
        webview.create_window(
            APP_TITLE,
            html="<body style='font:16px sans-serif;padding:40px;color:#b00'>"
            "Не удалось запустить сервер приложения. Попробуйте перезапустить программу.</body>",
            width=720, height=360,
        )
        webview.start()
        return 1

    webview.create_window(
        APP_TITLE, f"http://127.0.0.1:{port}/",
        width=1320, height=860, min_size=(960, 640),
    )
    webview.start()  # блокирует, пока окно открыто
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())

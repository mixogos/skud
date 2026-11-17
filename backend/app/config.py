"""Настройки приложения. Значения можно переопределить переменными окружения SKUD_*."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SKUD_DATA_DIR", BASE_DIR / "data"))
# Собранный веб-интерфейс (результат «npm run build» в папке frontend).
FRONTEND_DIR = Path(os.environ.get("SKUD_FRONTEND_DIR", BASE_DIR.parent / "frontend" / "dist"))

# Строка подключения SQLAlchemy. По умолчанию — файл SQLite в папке data.
DATABASE_URL = os.environ.get("SKUD_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'skud.db').as_posix()}")

# Время жизни сессии (токена) в часах; продлевается при каждом запросе.
SESSION_TTL_HOURS = int(os.environ.get("SKUD_SESSION_TTL_HOURS", "24"))

MIN_PASSWORD_LENGTH = 8

# Временный режим без входа: запросы с этого же компьютера (127.0.0.1) выполняются от имени
# администратора без логина и пароля. Запросам с других компьютеров токен нужен по-прежнему.
# Вернуть обязательный вход: переменная окружения SKUD_NO_AUTH=0.
AUTH_DISABLED = os.environ.get("SKUD_NO_AUTH", "1") != "0"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Автообновление через GitHub Releases: «владелец/репозиторий», откуда брать новые версии.
# Пусто — проверка обновлений отключена. Для приватного репозитория укажите токен в SKUD_UPDATE_TOKEN.
UPDATE_REPO = os.environ.get("SKUD_UPDATE_REPO", "mixogos/skud")
UPDATE_TOKEN = os.environ.get("SKUD_UPDATE_TOKEN", "")

# --- Распознавание лиц ---
MODELS_DIR = Path(os.environ.get("SKUD_MODELS_DIR", DATA_DIR / "models"))
# Ограничения на загружаемые фото (как в FindFace Multi).
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_SIDE = 6000
MIN_FACE_SIZE = int(os.environ.get("SKUD_MIN_FACE_SIZE", "50"))  # пикселей по меньшей стороне
DETECTION_THRESHOLD = float(os.environ.get("SKUD_DETECTION_THRESHOLD", "0.5"))
# Общий порог схожести (0..1), выше которого лица считаются одним человеком.
# Схожесть = (косинус + 1) / 2; 0.70 соответствует косинусу 0.40.
CONFIDENCE_THRESHOLD = float(os.environ.get("SKUD_CONFIDENCE_THRESHOLD", "0.70"))
# Сколько минут сервер помнит результат /detect/ для последующего /verify/.
DETECTION_TTL_MINUTES = 10
# --- Обработка видео ---
MAX_VIDEO_BYTES = int(os.environ.get("SKUD_MAX_VIDEO_MB", "4096")) * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".m4v", ".ts", ".mpg", ".mpeg", ".wmv", ".flv"}
# Запускать ли фоновый обработчик видео вместе с сервером.
VIDEO_WORKER_ENABLED = os.environ.get("SKUD_VIDEO_WORKER", "1") != "0"
# Запускать ли чтение потоков с камер вместе с сервером.
STREAMS_ENABLED = os.environ.get("SKUD_STREAMS", "1") != "0"
# Разрешить в качестве «камеры» видеофайл на сервере (проигрывается по кругу) — для проверки без камер.
CAMERA_FILE_SOURCES = os.environ.get("SKUD_CAMERA_FILE_SOURCES", "0") == "1"

# --- Вебхуки ---
WEBHOOK_TIMEOUT = float(os.environ.get("SKUD_WEBHOOK_TIMEOUT", "5"))
# Пауза перед повторной отправкой, секунд; с каждой попыткой удваивается.
WEBHOOK_RETRY_DELAY = float(os.environ.get("SKUD_WEBHOOK_RETRY_DELAY", "1"))

# Число потоков процессора для нейросетей (0 — выбрать автоматически).
INFERENCE_THREADS = int(os.environ.get("SKUD_INFERENCE_THREADS", "0"))

# Защита от подбора пароля: после N неудач подряд вход блокируется на LOCK_MINUTES.
MAX_FAILED_LOGINS = int(os.environ.get("SKUD_MAX_FAILED_LOGINS", "5"))
LOCK_MINUTES = int(os.environ.get("SKUD_LOCK_MINUTES", "5"))

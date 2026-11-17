"""Хранилище файлов (аналог findface-upload): исходные фото и миниатюры лиц."""
from pathlib import Path

from . import config

FACES_DIR = config.DATA_DIR / "uploads" / "faces"


VIDEOS_DIR = config.DATA_DIR / "uploads" / "videos"
EVENTS_DIR = config.DATA_DIR / "uploads" / "events"


def video_path(file_name: str) -> Path:
    # Имена файлов видео формирует сервер; на всякий случай отбрасываем путь.
    return VIDEOS_DIR / Path(file_name).name


def _event_path(event_id: int, kind: str) -> Path:
    assert kind in ("full", "thumb")
    # По тысяче файлов на папку, чтобы каталоги не разрастались.
    return EVENTS_DIR / f"{int(event_id) // 1000:06d}" / f"{int(event_id)}_{kind}.jpg"


def save_event_file(event_id: int, kind: str, data: bytes) -> None:
    path = _event_path(event_id, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def event_file(event_id: int, kind: str) -> Path | None:
    path = _event_path(event_id, kind)
    return path if path.is_file() else None


def delete_event_files(event_id: int) -> None:
    for kind in ("full", "thumb"):
        _event_path(event_id, kind).unlink(missing_ok=True)


COUNTERS_DIR = config.DATA_DIR / "uploads" / "counters"


def _counter_path(record_id: int) -> Path:
    return COUNTERS_DIR / f"{int(record_id) // 1000:06d}" / f"{int(record_id)}.jpg"


def save_counter_image(record_id: int, data: bytes) -> None:
    path = _counter_path(record_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def counter_image(record_id: int) -> Path | None:
    path = _counter_path(record_id)
    return path if path.is_file() else None


def delete_counter_image(record_id: int) -> None:
    _counter_path(record_id).unlink(missing_ok=True)


def _face_path(face_id: int, kind: str) -> Path:
    assert kind in ("src", "thumb")
    return FACES_DIR / f"{int(face_id)}_{kind}.jpg"


def save_face_file(face_id: int, kind: str, data: bytes) -> None:
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    _face_path(face_id, kind).write_bytes(data)


def face_file(face_id: int, kind: str) -> Path | None:
    path = _face_path(face_id, kind)
    return path if path.is_file() else None


def delete_face_files(face_id: int) -> None:
    for kind in ("src", "thumb"):
        _face_path(face_id, kind).unlink(missing_ok=True)

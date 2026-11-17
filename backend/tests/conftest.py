import os
import tempfile
import time
from pathlib import Path

# База для тестов — отдельная временная папка; задаётся до импорта приложения.
_tmp = tempfile.mkdtemp(prefix="skud-test-")
# Модели распознавания берём настоящие (они большие и не меняются).
os.environ.setdefault(
    "SKUD_MODELS_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "models")
)
os.environ["SKUD_DATA_DIR"] = _tmp
os.environ["SKUD_MAX_FAILED_LOGINS"] = "3"
os.environ["SKUD_NO_AUTH"] = "0"  # тесты проверяют обычный режим с обязательным входом

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import create_superadmin, init_db
from app.db import Base, SessionLocal, engine
from app.main import app
from app.recognition.index import face_index

ADMIN = {"name": "admin", "password": "admin-test-pass"}


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    init_db()
    face_index.invalidate()
    with SessionLocal() as db:
        create_superadmin(db, ADMIN["name"], ADMIN["password"])
    with TestClient(app) as c:
        yield c


def login(client, name, password):
    r = client.post("/auth/login/", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Token {r.json()['token']}"}


GROUP_PHOTO = Path(__file__).parent / "data" / "t1.jpg"  # шесть человек
VIDEO_FPS = 10


@pytest.fixture(scope="session")
def video_file(tmp_path_factory):
    """Видео 6 с: групповое фото медленно движется (2 с), пустой фон (2 с), снова фото (2 с).

    Ожидается два прохода по шесть человек — 12 треков.
    """
    import cv2
    import numpy as np

    from app.recognition import engine as eng

    photo = cv2.resize(eng.decode_image(GROUP_PHOTO.read_bytes()), (960, 664))
    h, w = photo.shape[:2]
    path = tmp_path_factory.mktemp("video") / "scene.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), VIDEO_FPS, (w, h))
    assert writer.isOpened()

    def moving(i, direction):
        shift = np.float32([[1, 0, direction * 3 * i], [0, 1, 2 * i]])
        return cv2.warpAffine(photo, shift, (w, h), borderValue=(90, 90, 90))

    for i in range(2 * VIDEO_FPS):
        writer.write(moving(i, 1))
    for _ in range(2 * VIDEO_FPS):
        writer.write(np.full((h, w, 3), 90, np.uint8))
    for i in range(2 * VIDEO_FPS):
        writer.write(moving(i, -1))
    writer.release()
    return path


def wait_for_video(client, admin, video_id, states, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        video = client.get(f"/videos/{video_id}/", headers=admin).json()
        if video["state"] in states:
            return video
        time.sleep(0.2)
    raise AssertionError(f"видео не перешло в {states}: {video}")


@pytest.fixture()
def admin(client):
    return login(client, **ADMIN)


@pytest.fixture()
def roles(client, admin):
    return {r["name"]: r for r in client.get("/groups/", headers=admin).json()["results"]}

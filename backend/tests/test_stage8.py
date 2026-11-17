"""Этап 8: потоки с камер и проверка живости лица."""
import time

import pytest

from app import config, events
from app.db import SessionLocal
from app.recognition import engine as eng
from app.recognition import liveness, model_store
from app.video import streams
from app.video.processor import TrackResult
from conftest import GROUP_PHOTO, login

needs_models = pytest.mark.skipif(
    not (model_store.models_present() and GROUP_PHOTO.is_file()),
    reason="нет моделей или тестового фото",
)
needs_liveness = pytest.mark.skipif(not model_store.liveness_present(), reason="нет моделей проверки живости")


@pytest.fixture(scope="module")
def people():
    image = eng.decode_image(GROUP_PHOTO.read_bytes())
    faces = sorted(eng.get_engine().detect_and_extract(image), key=lambda f: f.bbox[0])
    return {"image": image, "faces": faces}


def group_id(client, admin):
    return client.get("/camera-groups/", headers=admin).json()["results"][0]["id"]


def wait_camera(client, admin, camera_id, check, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        camera = client.get(f"/cameras/{camera_id}/", headers=admin).json()
        if check(camera):
            return camera
        time.sleep(0.2)
    raise AssertionError(f"камера не дошла до нужного состояния: {camera}")


# --- Адреса потоков (без нейросетей) ---
def test_stream_url_rules(monkeypatch, tmp_path):
    assert streams.validate_url(" rtsp://cam.local:554/stream1 ") == "rtsp://cam.local:554/stream1"
    assert streams.validate_url("http://10.0.0.5/video.mjpg") == "http://10.0.0.5/video.mjpg"
    assert streams.validate_url("0") == "0"
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    for bad in ("", "ftp://host/x", "file:///etc/passwd", "rtsp://", "12", str(video), r"\\server\share\a.mp4"):
        with pytest.raises(ValueError):
            streams.validate_url(bad)
    monkeypatch.setattr(config, "CAMERA_FILE_SOURCES", True)
    assert streams.validate_url(str(video)) == str(video)

    assert streams.mask_url("rtsp://admin:s3cret@10.0.0.5:554/h264?x=1") == "rtsp://admin:***@10.0.0.5:554/h264?x=1"
    assert streams.mask_url("rtsp://10.0.0.5/h264") == "rtsp://10.0.0.5/h264"
    assert streams.mask_url("0") == "0"


def test_camera_api_and_password_masking(client, admin, roles):
    group = group_id(client, admin)
    secret_url = "rtsp://admin:s3cret@127.0.0.1:1/stream"
    assert client.post("/cameras/", headers=admin, json={"name": "К", "group_id": group, "url": "ftp://x/y"}).status_code == 400
    bad_settings = {"name": "К", "group_id": group, "url": secret_url, "stream_settings": {"process_fps": 0}}
    assert client.post("/cameras/", headers=admin, json=bad_settings).status_code == 400

    r = client.post("/cameras/", headers=admin, json={
        "name": "Вход", "group_id": group, "url": secret_url, "active": False, "stream_settings": {"process_fps": 3},
    })
    assert r.status_code == 201, r.text
    camera = r.json()
    assert camera["external_detector"] is False and camera["token"] is None
    assert camera["url"] == "rtsp://admin:***@127.0.0.1:1/stream" and camera["state"] == "DISABLED"
    assert camera["stream_settings"] == {"process_fps": 3.0}
    assert "s3cret" not in client.get("/cameras/", headers=admin).text
    assert "s3cret" not in client.get("/auditlogs/", headers=admin).text

    # Адрес со скрытым паролем означает «не менять»; новый адрес заменяет прежний.
    client.patch(f"/cameras/{camera['id']}/", headers=admin, json={"url": camera["url"], "name": "Вход 1"})
    with SessionLocal() as db:
        from app import models
        assert db.get(models.Camera, camera["id"]).url == secret_url
    changed = client.patch(f"/cameras/{camera['id']}/", headers=admin, json={"url": "rtsp://127.0.0.1:1/other"}).json()
    assert changed["url"] == "rtsp://127.0.0.1:1/other" and changed["name"] == "Вход 1"

    # Камеры и детекторы — в одном списке, с отбором по виду.
    detector = client.post("/cameras/", headers=admin, json={"name": "Терминал", "group_id": group}).json()
    assert detector["external_detector"] is True and detector["token"]
    ids = lambda **params: [c["id"] for c in client.get("/cameras/", headers=admin, params=params).json()["results"]]
    assert ids() == [camera["id"], detector["id"]]
    assert ids(external_detector="false") == [camera["id"]] and ids(external_detector="true") == [detector["id"]]
    assert client.patch(f"/cameras/{detector['id']}/", headers=admin, json={"url": "rtsp://h/x"}).status_code == 400
    assert client.post(f"/cameras/{camera['id']}/token/", headers=admin).status_code == 404
    assert client.get(f"/cameras/{camera['id']}/snapshot/", headers=admin).status_code == 404

    # Недоступная камера: состояние «ошибка» с понятным сообщением, сервер продолжает работать.
    client.patch(f"/cameras/{camera['id']}/", headers=admin, json={"active": True})
    failed = wait_camera(client, admin, camera["id"], lambda c: c["state"] == "ERROR", timeout=40)
    assert "Не удалось подключиться" in failed["state_message"]
    off = client.patch(f"/cameras/{camera['id']}/", headers=admin, json={"active": False}).json()
    assert off["state"] == "DISABLED"

    client.post("/users/", headers=admin, json={"name": "viewer", "password": "viewer-pass-1", "role_id": roles["Пользователь"]["id"]})
    viewer = login(client, "viewer", "viewer-pass-1")
    assert client.post("/cameras/", headers=viewer, json={"name": "X", "group_id": group, "url": "rtsp://h/x"}).status_code == 403
    assert client.delete(f"/cameras/{camera['id']}/", headers=admin).status_code == 204
    assert ids() == [detector["id"]]


@needs_models
def test_camera_stream_creates_events(client, admin, video_file, monkeypatch):
    """Видеофайл в роли камеры: поток читается в реальном времени, события привязаны к камере."""
    monkeypatch.setattr(config, "CAMERA_FILE_SOURCES", True)
    group = group_id(client, admin)
    r = client.post("/cameras/", headers=admin, json={"name": "Проходная", "group_id": group, "url": str(video_file)})
    assert r.status_code == 201, r.text
    camera_id = r.json()["id"]
    try:
        camera = wait_camera(client, admin, camera_id, lambda c: c["events_created"] >= 6)
        assert camera["state"] == "ONLINE" and camera["frames_processed"] > 0
        assert (camera["width"], camera["height"]) == (960, 664) and camera["last_frame_date"]
        snapshot = client.get(f"/cameras/{camera_id}/snapshot/", headers=admin)
        assert snapshot.status_code == 200 and snapshot.content[:3] == b"\xff\xd8\xff"
        assert client.get(f"/cameras/{camera_id}/snapshot/").status_code == 401

        found = client.get("/events/faces/", headers=admin, params={"cameras": [camera_id]}).json()
        assert found["count"] >= 6
        event = found["results"][0]
        assert event["camera_id"] == camera_id and event["camera_group_id"] == group
        assert event["video_archive_id"] is None and event["video_offset"] is None
    finally:
        stopped = client.patch(f"/cameras/{camera_id}/", headers=admin, json={"active": False}).json()
    assert stopped["state"] == "DISABLED" and stopped["frames_processed"] == 0
    count = client.get("/events/faces/", headers=admin, params={"cameras": [camera_id]}).json()["count"]
    time.sleep(1.5)  # после выключения новых событий нет
    assert client.get("/events/faces/", headers=admin, params={"cameras": [camera_id]}).json()["count"] == count


# --- Живость ---
@needs_models
@needs_liveness
def test_liveness_scores_real_faces(people):
    checker = liveness.get_liveness()
    scores = [checker.score(people["image"], face.bbox) for face in people["faces"]]
    assert all(0.5 < s <= 1 for s in scores), scores


@needs_models
@needs_liveness
def test_liveness_in_access_decision(client, admin, people, monkeypatch):
    group = group_id(client, admin)
    staff = client.post("/watch-lists/", headers=admin, json={"name": "Персонал", "access_policy": "allow"}).json()["id"]
    card = client.post("/cards/humans/", headers=admin, json={"name": "Сотрудник", "watch_lists": [staff]}).json()["id"]
    portrait = eng.make_thumbnail(people["image"], people["faces"][0].bbox, margin=1.0, max_side=600)
    r = client.post("/objects/faces/", headers=admin, data={"card": card}, files={"source_photo": ("p.jpg", portrait, "image/jpeg")})
    assert r.status_code == 201, r.text
    client.patch(f"/camera-groups/{group}/", headers=admin, json={"access_control": True})

    def register():
        result = TrackResult(1, people["faces"][0], people["image"], 0.8, 0.0, 0.0, 1.0, 5)
        with SessionLocal() as db:
            from app import models
            reg = events.process_track(db, result, created_date=models.utcnow(), camera_group_id=group)
            db.commit()
            return reg.event.id, reg.access, reg.access_reason

    # Проверка живости в группе выключена: оценка записывается, но на решение не влияет.
    client.patch("/settings/", headers=admin, json={"liveness_threshold": 1.0})
    event_id, decision, _ = register()
    assert decision == "granted"
    event = client.get(f"/events/faces/{event_id}/", headers=admin).json()
    assert 0.5 < event["liveness"] < 1

    # Включена: лицо с оценкой ниже порога не проходит, даже если человек в разрешающем списке.
    updated = client.patch(f"/camera-groups/{group}/", headers=admin, json={"liveness_check": True}).json()
    assert updated["liveness_check"] is True
    _, decision, reason = register()
    assert decision == "denied" and "подделку" in reason
    client.patch("/settings/", headers=admin, json={"liveness_threshold": 0.5})
    real_id, decision, reason = register()
    assert decision == "granted", reason

    # Отбор событий по живости (порог 0,5: все три события — настоящие лица).
    count = lambda value: client.get("/events/faces/", headers=admin, params={"liveness": value}).json()["count"]
    assert count("real") == 3 and count("fake") == 0
    client.patch("/settings/", headers=admin, json={"liveness_threshold": 1.0})
    assert count("real") == 0 and count("fake") == 3
    assert client.patch("/settings/", headers=admin, json={"liveness_threshold": 1.5}).status_code == 422

    # Моделей нет — при включённой проверке дверь не открывается.
    client.patch("/settings/", headers=admin, json={"liveness_threshold": 0.5})
    monkeypatch.setattr(events, "get_liveness", lambda: None)
    _, decision, reason = register()
    assert decision == "denied" and "недоступна" in reason


# --- Режим без входа ---
def test_no_auth_mode_is_local_only(client, admin, monkeypatch):
    assert client.get("/users/me/").status_code == 401
    monkeypatch.setattr(config, "AUTH_DISABLED", True)
    assert client.get("/users/me/").status_code == 401            # запрос не с этого компьютера
    monkeypatch.setattr(config, "LOCAL_HOSTS", {"testclient"})
    me = client.get("/users/me/")
    assert me.status_code == 200 and me.json()["name"] == "admin"
    assert client.get("/users/me/", headers={"Authorization": "Token wrong"}).status_code == 200
    assert client.post("/watch-lists/", json={"name": "Без входа"}).status_code == 201
    assert client.get("/users/me/", headers=admin).status_code == 200   # обычный токен продолжает работать
    monkeypatch.setattr(config, "AUTH_DISABLED", False)
    assert client.get("/users/me/").status_code == 401

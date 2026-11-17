"""Этап 9: атрибуты лица, счётчики и пересечение линий."""
import time
from datetime import timedelta

import cv2
import numpy as np
import pytest

from app import config, events, models
from app.db import SessionLocal
from app.recognition import attributes, model_store
from app.recognition import engine as eng
from app.video.analytics import crossing_direction, in_roi
from app.video.processor import TrackResult
from app.video.tracker import FaceTracker, StreamSettings
from conftest import GROUP_PHOTO, login

needs_models = pytest.mark.skipif(
    not (model_store.models_present() and GROUP_PHOTO.is_file()), reason="нет моделей или тестового фото"
)
needs_attributes = pytest.mark.skipif(
    not (model_store.genderage_present() and model_store.mask_present()), reason="нет моделей атрибутов"
)


@pytest.fixture(scope="module")
def people():
    image = eng.decode_image(GROUP_PHOTO.read_bytes())
    faces = sorted(eng.get_engine().detect_and_extract(image), key=lambda f: f.bbox[0])
    return {"image": image, "faces": faces}


def wait_until(check, timeout=60, what="условие"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"не дождались: {what}")


# --- Атрибуты ---
@needs_models
@needs_attributes
def test_attributes_of_real_faces(people):
    analyzer = attributes.get_attributes()
    found = [analyzer.analyze(people["image"], face.bbox) for face in people["faces"]]
    assert [f["gender"]["name"] for f in found] == ["female", "female", "male", "male", "male", "female"]
    assert all(18 <= f["age"] <= 55 for f in found), found
    assert all(f["medmask"]["name"] == "none" and f["medmask"]["confidence"] > 0.9 for f in found), found

    # Нижняя половина лица закрыта светлой «маской».
    face = people["faces"][0]
    left, top, right, bottom = face.bbox
    nose_y = int(face.landmarks[2][1]) - 4
    masked = people["image"].copy()
    shape = np.array([[left, nose_y], [right, nose_y], [right - 6, bottom - 4], [(left + right) // 2, bottom + 4], [left + 6, bottom - 4]], np.int32)
    cv2.fillPoly(masked, [shape], (235, 225, 200))
    assert analyzer.analyze(masked, face.bbox)["medmask"]["name"] == "correct"
    assert analyzer.analyze(people["image"], (5, 5, 6, 6)) == {}


@needs_models
@needs_attributes
def test_event_attributes_and_filters(client, admin, people):
    group = client.get("/camera-groups/", headers=admin).json()["results"][0]["id"]
    with SessionLocal() as db:
        for i, face in enumerate(people["faces"]):
            result = TrackResult(i, face, people["image"], 0.8, 0.0, 0.0, 1.0, 5)
            events.process_track(db, result, created_date=models.utcnow(), camera_group_id=group)
        db.commit()

    def count(**params):
        return client.get("/events/faces/", headers=admin, params=params).json()["count"]

    event = client.get("/events/faces/", headers=admin).json()["results"][0]
    assert set(event["features"]) == {"age", "gender", "medmask"}
    assert count() == 6 and count(gender="female") == 3 and count(gender="male") == 3
    assert count(medmask="none") == 6 and count(medmask="correct") == 0
    ages = sorted(e["features"]["age"] for e in client.get("/events/faces/", headers=admin).json()["results"])
    assert count(age_gte=ages[2]) == sum(a >= ages[2] for a in ages)
    assert count(age_lte=ages[2]) == sum(a <= ages[2] for a in ages)
    assert count(age_gte=ages[0], age_lte=ages[-1], gender="male") == 3
    assert client.get("/events/faces/", headers=admin, params={"age_gte": 101}).status_code == 422


# --- Геометрия и трекер (без нейросетей) ---
def test_line_geometry():
    line = [[0.5, 0.0], [0.5, 1.0]]  # сверху вниз: A — справа на картинке, B — слева
    assert crossing_direction(line, (0.4, 0.5), (0.6, 0.5)) == "BA"
    assert crossing_direction(line, (0.6, 0.5), (0.4, 0.5)) == "AB"
    assert crossing_direction(line, (0.4, 0.5), (0.45, 0.9)) is None      # не дошёл до линии
    short = [[0.5, 0.0], [0.5, 0.3]]
    assert crossing_direction(short, (0.4, 0.5), (0.6, 0.5)) is None      # прошёл ниже отрезка
    assert crossing_direction(short, (0.4, 0.1), (0.6, 0.2)) == "BA"
    horizontal = [[0.0, 0.5], [1.0, 0.5]]  # слева направо: A — сверху, B — снизу
    assert crossing_direction(horizontal, (0.3, 0.4), (0.3, 0.6)) == "AB"
    assert in_roi([], (0.9, 0.9)) and in_roi([0, 0, 0.5, 1], (0.4, 0.2)) and not in_roi([0, 0, 0.5, 1], (0.6, 0.2))


def test_tracker_keeps_trajectory_when_track_is_cut():
    face = lambda x: (eng.Face((x, 100, x + 80, 180), 0.9, np.zeros((5, 2), np.float32)), 0.8)
    frame = np.zeros((10, 10, 3), np.uint8)
    tracker = FaceTracker(StreamSettings(max_track_duration=0.4))
    finished = []
    for i in range(6):
        finished += tracker.update(frame, [face(100 + 10 * i)], i * 0.2)
        (alive,) = tracker.tracks
        if i:  # человек всё время один: у живого трека есть прошлое положение
            assert alive.prev_bbox is not None or alive.frames == 0
    assert len(finished) == 2 and all(t.successor_id for t in finished)
    assert finished[0].successor_id == finished[1].id
    # Продолжение, к которому человек не вернулся, события не даёт: в нём нет ни одного кадра.
    tail = tracker.flush()
    assert all(t.frames >= 1 or t.best_face is None for t in tail)


# --- API ---
def test_analytics_api(client, admin, roles):
    group = client.get("/camera-groups/", headers=admin).json()["results"][0]["id"]
    camera = client.post("/cameras/", headers=admin, json={"name": "Зал", "group_id": group, "url": "rtsp://127.0.0.1:1/x", "active": False}).json()["id"]
    detector = client.post("/cameras/", headers=admin, json={"name": "Терминал", "group_id": group}).json()["id"]

    post = lambda path, **body: client.post(path, headers=admin, json=body)
    assert post("/counters/", name="С", camera_id=999).status_code == 400
    assert post("/counters/", name="С", camera_id=detector).status_code == 400
    assert post("/counters/", name="С", camera_id=camera, roi=[0.1, 0.1, 0.5]).status_code == 422
    assert post("/counters/", name="С", camera_id=camera, roi=[0.1, 0.1, 1.5, 0.9]).status_code == 422
    assert post("/counters/", name="С", camera_id=camera, roi=[0.5, 0.1, 0.5, 0.9]).status_code == 422
    assert post("/counters/", name="С", camera_id=camera, count_interval=0).status_code == 422
    r = post("/counters/", name="Очередь", camera_id=camera, roi=[0, 0, 0.5, 1], count_interval=5)
    assert r.status_code == 201, r.text
    counter = r.json()
    assert counter["roi"] == [0, 0, 0.5, 1] and counter["last_count"] is None and counter["active"] is True
    changed = client.patch(f"/counters/{counter['id']}/", headers=admin, json={"roi": [], "name": "Зал целиком"}).json()
    assert changed["roi"] == [] and changed["name"] == "Зал целиком" and changed["count_interval"] == 5
    assert client.get("/counters/", headers=admin, params={"cameras": [camera]}).json()["count"] == 1
    assert client.get("/counters/", headers=admin, params={"cameras": [detector]}).json()["count"] == 0

    assert post("/lines/", name="Л", camera_id=camera, points=[[0.5, 0.1]]).status_code == 422
    assert post("/lines/", name="Л", camera_id=camera, points=[[0.5, 0.1], [0.5, 0.11]]).status_code == 422
    assert post("/lines/", name="Л", camera_id=camera, points=[[0.5, 0.1], [0.5, 1.2]]).status_code == 422
    assert post("/lines/", name="Л", camera_id=detector, points=[[0.5, 0], [0.5, 1]]).status_code == 400
    r = post("/lines/", name="Вход", camera_id=camera, points=[[0.5, 0], [0.5, 1]])
    assert r.status_code == 201, r.text
    line = r.json()
    assert line["points"] == [[0.5, 0], [0.5, 1]] and (line["count_ab"], line["count_ba"]) == (0, 0)

    # Записи и пересечения, как их оставил бы поток камеры.
    now = models.utcnow()
    with SessionLocal() as db:
        for minutes, faces in ((50, 2), (40, 4), (30, 0), (5, 3)):
            db.add(models.CounterRecord(counter_id=counter["id"], created_date=now - timedelta(minutes=minutes), faces_count=faces))
        for minutes, direction in ((90, "AB"), (20, "AB"), (10, "BA"), (1, "AB")):
            db.add(models.LineCrossing(line_id=line["id"], created_date=now - timedelta(minutes=minutes), direction=direction))
        db.commit()
    got = client.get(f"/counters/{counter['id']}/", headers=admin).json()
    assert got["last_count"] == 3 and got["last_date"]
    records = lambda **p: client.get("/counter-records/", headers=admin, params={"counter": counter["id"], **p}).json()
    assert records()["count"] == 4 and [r["faces_count"] for r in records()["results"]] == [3, 0, 4, 2]
    assert records(count_gte=3)["count"] == 2 and records(count_lte=0)["count"] == 1
    assert records(created_date_gte=(now - timedelta(minutes=35)).isoformat())["count"] == 2
    chart = client.get(f"/counters/{counter['id']}/chart/", headers=admin, params={
        "created_date_gte": (now - timedelta(minutes=60)).isoformat(), "created_date_lte": now.isoformat(), "points": 2,
    }).json()
    assert [(p["average"], p["maximum"], p["records"]) for p in chart] == [(3.0, 4, 2), (1.5, 3, 2)]
    assert client.get(f"/counter-records/{records()['results'][0]['id']}/image/", headers=admin).status_code == 404

    lines = lambda **p: client.get("/lines/", headers=admin, params=p).json()["results"][0]
    assert (lines()["count_ab"], lines()["count_ba"]) == (3, 1)
    since = lines(since=(now - timedelta(minutes=30)).isoformat())
    assert (since["count_ab"], since["count_ba"]) == (2, 1)
    crossings = lambda **p: client.get("/line-crossings/", headers=admin, params={"line": line["id"], **p}).json()
    assert crossings()["count"] == 4 and crossings(direction="BA")["count"] == 1
    assert [c["direction"] for c in crossings()["results"]] == ["AB", "BA", "AB", "AB"]

    # Права: «Пользователь» только смотрит.
    client.post("/users/", headers=admin, json={"name": "viewer", "password": "viewer-pass-1", "role_id": roles["Пользователь"]["id"]})
    viewer = login(client, "viewer", "viewer-pass-1")
    assert client.get("/counters/", headers=viewer).status_code == 200 and client.get("/lines/", headers=viewer).status_code == 200
    assert client.post("/lines/", headers=viewer, json={"name": "Л", "camera_id": camera, "points": [[0, 0], [1, 1]]}).status_code == 403
    assert client.delete(f"/counters/{counter['id']}/", headers=viewer).status_code == 403

    assert client.delete(f"/counters/{counter['id']}/records/", headers=admin).json() == {"deleted": 4}
    assert records()["count"] == 0
    # Вместе с камерой исчезают её счётчики, линии и их данные.
    assert client.delete(f"/cameras/{camera}/", headers=admin).status_code == 204
    assert client.get("/counters/", headers=admin).json()["count"] == 0 and client.get("/lines/", headers=admin).json()["count"] == 0
    assert crossings()["count"] == 0


@needs_models
def test_stream_counts_faces_and_line_crossings(client, admin, video_file, people, monkeypatch):
    """Видеофайл в роли камеры: шесть человек сдвигаются вправо, пустой кадр, сдвигаются влево."""
    monkeypatch.setattr(config, "CAMERA_FILE_SOURCES", True)
    group = client.get("/camera-groups/", headers=admin).json()["results"][0]["id"]
    watch_list = client.get("/watch-lists/", headers=admin).json()["results"][0]["id"]
    card = client.post("/cards/humans/", headers=admin, json={"name": "Первая слева", "watch_lists": [watch_list]}).json()["id"]
    portrait = eng.make_thumbnail(people["image"], people["faces"][0].bbox, margin=1.0, max_side=600)
    r = client.post("/objects/faces/", headers=admin, data={"card": card}, files={"source_photo": ("p.jpg", portrait, "image/jpeg")})
    assert r.status_code == 201, r.text

    camera = client.post("/cameras/", headers=admin, json={"name": "Зал", "group_id": group, "url": str(video_file), "active": False}).json()["id"]
    whole = client.post("/counters/", headers=admin, json={"name": "Весь кадр", "camera_id": camera, "count_interval": 1}).json()["id"]
    left = client.post("/counters/", headers=admin, json={"name": "Левая половина", "camera_id": camera, "count_interval": 1, "roi": [0, 0, 0.5, 1]}).json()["id"]
    # Лицо первого человека (центр x ≈ 0,09) при сдвиге вправо доходит до 0,15 — и пересекает линию x = 0,115.
    line = client.post("/lines/", headers=admin, json={"name": "Слева", "camera_id": camera, "points": [[0.115, 0], [0.115, 1]]}).json()["id"]
    far = client.post("/lines/", headers=admin, json={"name": "Внизу", "camera_id": camera, "points": [[0, 0.98], [1, 0.98]]}).json()["id"]
    client.patch(f"/cameras/{camera}/", headers=admin, json={"active": True})
    try:
        crossings = lambda **p: client.get("/line-crossings/", headers=admin, params=p).json()
        wait_until(lambda: crossings(line=line, matched_card=card)["count"] >= 1, what="пересечение линии с распознанным человеком")
        records = lambda counter, **p: client.get("/counter-records/", headers=admin, params={"counter": counter, **p}).json()
        wait_until(lambda: records(whole, count_gte=6)["count"] >= 1 and records(left, count_gte=3)["count"] >= 1, what="записи счётчиков")
        # Видео идёт по кругу (6 с): счёт продолжается и после переподключения к источнику.
        wait_until(lambda: records(whole)["count"] >= 9, what="записи счётчика после перезапуска потока")
    finally:
        client.patch(f"/cameras/{camera}/", headers=admin, json={"active": False})

    found = crossings(line=line)
    assert {c["direction"] for c in found["results"]} == {"BA"}       # слева направо через вертикальную линию
    linked = crossings(line=line, matched_card=card)["results"][0]
    assert linked["matched_card_name"] == "Первая слева" and linked["face_event_id"]
    event = client.get(f"/events/faces/{linked['face_event_id']}/", headers=admin).json()
    assert event["matched_card_id"] == card and event["camera_id"] == camera
    assert crossings(line=far)["count"] == 0
    summary = client.get("/lines/", headers=admin, params={"cameras": [camera]}).json()["results"]
    assert summary[0]["count_ba"] == found["count"] and summary[0]["count_ab"] == 0

    assert {r["faces_count"] for r in records(whole, limit=500)["results"]} <= {0, 6}
    assert {r["faces_count"] for r in records(left, limit=500)["results"]} <= {0, 3}
    record = records(whole, count_gte=6)["results"][0]
    assert record["has_image"] and len(record["faces_bbox"]) == 6
    image = client.get(f"/counter-records/{record['id']}/image/", headers=admin)
    assert image.status_code == 200 and image.content[:3] == b"\xff\xd8\xff"
    assert client.get(f"/counter-records/{record['id']}/image/").status_code == 401
    empty = records(whole, count_lte=0)["results"]
    assert all(not r["has_image"] for r in empty)
    assert client.get(f"/counters/{whole}/", headers=admin).json()["last_count"] in (0, 6)
    # Удаление счётчика убирает его записи и снимки.
    assert client.delete(f"/counters/{whole}/", headers=admin).status_code == 204
    assert records(whole)["count"] == 0
    assert client.get(f"/counter-records/{record['id']}/image/", headers=admin).status_code == 404

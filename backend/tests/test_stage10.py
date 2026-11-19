"""Охранная аналитика: зоны, саботаж, вторжение, праздношатание, скопление, API."""
from datetime import datetime, timedelta

import numpy as np

from app import models
from app.db import SessionLocal
from app.recognition.people import PersonBox
from app.video import security as sec

WHEN = datetime(2026, 1, 1, 12, 0, 0)
ZONE = [[0.3, 0.3], [0.7, 0.3], [0.7, 0.95], [0.3, 0.95]]  # нижняя центральная часть кадра


def test_point_in_polygon():
    sq = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    assert sec.point_in_polygon(sq, (0.5, 0.5)) is True
    assert sec.point_in_polygon(sq, (0.1, 0.5)) is False
    assert sec.point_in_polygon(sq, (0.5, 0.9)) is False
    assert sec.point_in_polygon([], (0.9, 0.9)) is True  # пустая зона — весь кадр


def test_tamper_detector_cover_and_normal():
    det = sec.TamperDetector()
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    t = 0.0
    for _ in range(16):  # прогрев на нормальном текстурном кадре — тревоги нет
        assert det.check(base, t, 50) is None
        t += 0.5
    dark = np.zeros((480, 640, 3), np.uint8)
    fired = None
    for _ in range(12):  # обзор перекрыт: тревога после выдержки HOLD
        r = det.check(dark, t, 50)
        t += 0.5
        if r:
            fired = r
            break
    assert fired == "перекрыт обзор"


def _make_camera_and_rule(rule_type, params=None, zone=ZONE):
    with SessionLocal() as db:
        group = models.CameraGroup(name=f"g-{rule_type}")
        db.add(group)
        db.flush()
        camera = models.Camera(name=f"c-{rule_type}", group_id=group.id, url="file://x")
        db.add(camera)
        db.flush()
        rule = models.SecurityRule(
            name=f"R-{rule_type}", camera_id=camera.id, rule_type=rule_type,
            zone=(zone if rule_type != "tampering" else []), params=params or {},
        )
        db.add(rule)
        db.commit()
        return camera.id


def _events(camera_id, rule_type):
    with SessionLocal() as db:
        return db.query(models.SecurityEvent).filter_by(camera_id=camera_id, rule_type=rule_type).count()


def test_intrusion_fires_once_per_presence(client):
    camera_id = _make_camera_and_rule("intrusion")
    w = sec.SecurityWatcher(camera_id)
    w._reload()
    frame = np.zeros((720, 1280, 3), np.uint8)
    inside = (600, 300, 740, 660)  # центр по x ~0.52, низ ~0.92 — внутри зоны
    for i in range(5):  # один и тот же человек в зоне несколько кадров
        t = i * 0.4
        w._tracker.update(frame, [PersonBox(inside, 0.9)], t)
        w._check_persons(frame, t, WHEN + timedelta(seconds=t))
    assert _events(camera_id, "intrusion") == 1  # тревога один раз, не на каждом кадре


def test_intrusion_ignores_outside_zone(client):
    camera_id = _make_camera_and_rule("intrusion")
    w = sec.SecurityWatcher(camera_id)
    w._reload()
    frame = np.zeros((720, 1280, 3), np.uint8)
    outside = (30, 300, 170, 660)  # слева от зоны
    for i in range(5):
        t = i * 0.4
        w._tracker.update(frame, [PersonBox(outside, 0.9)], t)
        w._check_persons(frame, t, WHEN + timedelta(seconds=t))
    assert _events(camera_id, "intrusion") == 0


def test_loitering_after_dwell(client):
    camera_id = _make_camera_and_rule("loitering", {"dwell_seconds": 1})
    w = sec.SecurityWatcher(camera_id)
    w._reload()
    frame = np.zeros((720, 1280, 3), np.uint8)
    inside = (600, 300, 740, 660)
    for i in range(6):  # 6 кадров по 0.4 с = 2 с, порог 1 с
        t = i * 0.4
        w._tracker.update(frame, [PersonBox(inside, 0.9)], t)
        w._check_persons(frame, t, WHEN + timedelta(seconds=t))
    assert _events(camera_id, "loitering") == 1


def test_crowd_over_threshold(client):
    camera_id = _make_camera_and_rule("crowd", {"min_count": 2, "hold_seconds": 0})
    w = sec.SecurityWatcher(camera_id)
    w._reload()
    frame = np.zeros((720, 1280, 3), np.uint8)
    a, b = (480, 300, 560, 660), (820, 300, 900, 660)  # двое в зоне
    for i in range(3):
        t = i * 0.4
        w._tracker.update(frame, [PersonBox(a, 0.9), PersonBox(b, 0.9)], t)
        w._check_persons(frame, t, WHEN + timedelta(seconds=t))
    assert _events(camera_id, "crowd") == 1  # один раз (далее срабатывает выдержка кулдауна)


def test_security_rule_api(client, admin):
    group = client.post("/camera-groups/", json={"name": "Охрана"}, headers=admin).json()
    cam = client.post(
        "/cameras/", json={"name": "Вход", "group_id": group["id"], "url": "http://example.com/stream"}, headers=admin
    )
    assert cam.status_code == 201, cam.text
    camera_id = cam.json()["id"]
    # вторжение без зоны — ошибка
    bad = client.post("/security-rules/", json={
        "name": "Без зоны", "camera_id": camera_id, "rule_type": "intrusion"}, headers=admin)
    assert bad.status_code == 422
    # корректное правило
    good = client.post("/security-rules/", json={
        "name": "Склад", "camera_id": camera_id, "rule_type": "intrusion", "zone": ZONE}, headers=admin)
    assert good.status_code == 201, good.text
    assert good.json()["zone"] == ZONE
    listing = client.get("/security-rules/", params={"cameras": [camera_id]}, headers=admin).json()
    assert listing["count"] == 1
    # тревога и её подтверждение
    with SessionLocal() as db:
        ev = models.SecurityEvent(camera_id=camera_id, rule_type="intrusion", rule_name="Склад",
                                  severity="alarm", message="вход в зону")
        db.add(ev)
        db.commit()
        ev_id = ev.id
    events = client.get("/events/security/", params={"cameras": [camera_id]}, headers=admin).json()
    assert events["count"] == 1 and events["results"][0]["acknowledged"] is False
    ack = client.post(f"/events/security/{ev_id}/acknowledge/", headers=admin).json()
    assert ack["acknowledged"] is True

"""Удержание человека (дедупликация по эмбеддингу) и отсев картин/фото по живости.

Тесты драйвят events.process_track напрямую с готовыми векторами и оценкой живости —
нейросети лиц не нужны (эмбеддинг задаётся вручную).
"""
from datetime import timedelta

import numpy as np

from app import events, models
from app.db import SessionLocal
from app.recognition.engine import Face
from app.video.processor import TrackResult


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(512).astype(np.float32)
    return v / np.linalg.norm(v)


def _result(embedding, liveness=None) -> TrackResult:
    frame = np.full((200, 200, 3), 120, np.uint8)
    landmarks = np.array([[70, 60], [110, 60], [90, 85], [74, 120], [106, 120]], np.float32)
    face = Face(bbox=(60, 40, 120, 150), detection_score=0.95, landmarks=landmarks,
                embedding=np.asarray(embedding, dtype=np.float32))
    return TrackResult(track_id=1, face=face, frame=frame, quality=0.8, time=0.0,
                       first_time=0.0, duration=1.0, frames=5, liveness=liveness)


def _default_group(client, admin) -> int:
    return client.get("/camera-groups/", headers=admin).json()["results"][0]["id"]


def _run(group_id, result, when):
    with SessionLocal() as db:
        reg = events.process_track(db, result, created_date=when, camera_group_id=group_id)
        db.commit()
        return reg


def test_dedup_window_holds_person(client, admin, monkeypatch):
    monkeypatch.setattr(events, "get_attributes", lambda: None)  # не трогаем модель атрибутов
    group = _default_group(client, admin)
    assert client.patch("/settings/", headers=admin, json={"dedup_window": 30, "unmatched_liveness_filter": False}).status_code == 200
    person, base = _vec(1), models.utcnow()

    assert _run(group, _result(person), base).event is not None                       # первое появление
    held = _run(group, _result(person), base + timedelta(seconds=10))                  # то же лицо в окне
    assert held.event is None and "дедуплик" in held.skipped.lower()
    assert _run(group, _result(person), base + timedelta(seconds=45)).event is not None  # окно прошло — снова событие
    assert _run(group, _result(_vec(2)), base + timedelta(seconds=12)).event is not None  # другой человек — своё событие

    # Выключенное окно не подавляет ничего.
    client.patch("/settings/", headers=admin, json={"dedup_window": 0})
    assert _run(group, _result(person), base + timedelta(seconds=50)).event is not None
    assert _run(group, _result(person), base + timedelta(seconds=51)).event is not None


def test_liveness_filter_rejects_flat(client, admin, monkeypatch):
    monkeypatch.setattr(events, "get_attributes", lambda: None)
    group = _default_group(client, admin)
    client.patch("/settings/", headers=admin, json={"unmatched_liveness_filter": True, "unmatched_liveness_min": 0.4, "dedup_window": 0})

    flat = _run(group, _result(_vec(3), liveness=0.1), models.utcnow())   # картина/фото — низкая живость
    assert flat.event is None and "подделк" in flat.skipped.lower()
    assert _run(group, _result(_vec(4), liveness=0.9), models.utcnow()).event is not None  # живое лицо проходит

    # Фильтр выключен — пропускаем и низкую живость.
    client.patch("/settings/", headers=admin, json={"unmatched_liveness_filter": False})
    assert _run(group, _result(_vec(5), liveness=0.1), models.utcnow()).event is not None


def test_new_settings_validation(client, admin):
    assert client.patch("/settings/", headers=admin, json={"dedup_window": -1}).status_code == 422
    assert client.patch("/settings/", headers=admin, json={"dedup_window": 5000}).status_code == 422
    assert client.patch("/settings/", headers=admin, json={"unmatched_liveness_min": 1.5}).status_code == 422
    r = client.patch("/settings/", headers=admin, json={"dedup_window": 20, "unmatched_liveness_filter": True, "unmatched_liveness_min": 0.4})
    assert r.status_code == 200 and r.json()["dedup_window"] == 20 and r.json()["unmatched_liveness_min"] == 0.4

from datetime import datetime, timedelta

import cv2
import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from app import events, models
from app.db import SessionLocal
from app.recognition import engine as eng
from app.recognition import model_store
from app.video.processor import TrackResult
from conftest import GROUP_PHOTO, login, wait_for_video

pytestmark = pytest.mark.skipif(
    not (model_store.models_present() and GROUP_PHOTO.is_file()),
    reason="нет моделей или тестового фото",
)

T0 = datetime(2026, 1, 15, 9, 0, 0)


@pytest.fixture(scope="module")
def people():
    """Шесть человек с группового фото: кадр, лица с векторами и портреты для карточек.

    Второй набор лиц снят с искажённой копии фото — тот же человек, но схожесть ниже 1.
    """
    image = eng.decode_image(GROUP_PHOTO.read_bytes())
    engine = eng.get_engine()
    faces = sorted(engine.detect_and_extract(image), key=lambda f: f.bbox[0])
    h, w = image.shape[:2]
    portraits = []
    for f in faces:
        left, top, right, bottom = f.bbox
        mx, my = right - left, bottom - top
        portraits.append(eng.encode_jpeg(image[max(0, top - my):min(h, bottom + my), max(0, left - mx // 2):min(w, right + mx // 2)]))
    rot = cv2.getRotationMatrix2D((w / 2, h / 2), 6, 0.85)
    other_image = cv2.GaussianBlur(cv2.convertScaleAbs(cv2.warpAffine(image, rot, (w, h)), alpha=0.8, beta=10), (3, 3), 0)
    found = engine.detect_and_extract(other_image)
    other = [max(found, key=lambda g: eng.confidence(f.embedding, g.embedding)) for f in faces]
    return {"image": image, "faces": faces, "portraits": portraits, "other_image": other_image, "other": other}


def default_ids(client, admin):
    wl = client.get("/watch-lists/", headers=admin).json()["results"][0]["id"]
    group = client.get("/camera-groups/", headers=admin).json()["results"][0]["id"]
    return wl, group


def make_card(client, admin, name, lists, portrait, **extra):
    card = client.post("/cards/humans/", headers=admin, json={"name": name, "watch_lists": lists, **extra}).json()
    r = client.post("/objects/faces/", headers=admin, data={"card": card["id"]}, files={"source_photo": ("p.jpg", portrait, "image/jpeg")})
    assert r.status_code == 201, r.text
    return card["id"]


def register(people, person, when, group, variant="faces", publish=False, quality=0.8):
    """Имитирует завершившийся трек: человек person прошёл перед камерой в момент when."""
    image = people["image"] if variant == "faces" else people["other_image"]
    result = TrackResult(1, people[variant][person], image, quality, 0.0, 0.0, 1.0, 5)
    with SessionLocal() as db:
        event = events.register_track(db, result, created_date=when, camera_group_id=group)
        db.commit()
        if event is None:
            return None
        if publish:
            events.publish_event(db, event, created=True)
        return event.id


def get_event(client, admin, event_id):
    return client.get(f"/events/faces/{event_id}/", headers=admin).json()


# --- Сопоставление с картотекой ---
def test_matching_with_card_file(client, admin, people):
    wl, group = default_ids(client, admin)
    card = make_card(client, admin, "Иванов Иван", [wl], people["portraits"][0])

    matched = get_event(client, admin, register(people, 0, T0, group))
    assert matched["matched"] is True and matched["matched_card_id"] == card
    assert matched["matched_card_name"] == "Иванов Иван" and matched["matched_lists"] == [wl]
    assert matched["confidence"] > 0.9 and matched["matched_face_id"] and matched["acknowledged"] is True

    stranger = get_event(client, admin, register(people, 5, T0, group))
    assert stranger["matched"] is False and stranger["matched_card_id"] is None and stranger["confidence"] is None
    assert stranger["matched_lists"] == [] and stranger["episode_id"] != matched["episode_id"]


def test_thresholds_list_group_and_generic(client, admin, people):
    wl, group = default_ids(client, admin)
    make_card(client, admin, "Иванов", [wl], people["portraits"][0])

    def is_matched(when):
        return get_event(client, admin, register(people, 0, when, group, variant="other"))["matched"]

    base = get_event(client, admin, register(people, 0, T0, group, variant="other"))
    assert base["matched"] is True and 0.75 < base["confidence"] < 0.999  # искажённое фото: совпадение не идеальное
    strict = min(0.9999, base["confidence"] + 0.0005)

    # Порог группы камер выше схожести — совпадения нет.
    client.patch(f"/camera-groups/{group}/", headers=admin, json={"face_threshold": strict})
    assert is_matched(T0 + timedelta(hours=1)) is False
    # Собственный порог списка важнее порога группы.
    client.patch(f"/watch-lists/{wl}/", headers=admin, json={"face_threshold": 0.6})
    assert is_matched(T0 + timedelta(hours=2)) is True
    client.patch(f"/watch-lists/{wl}/", headers=admin, json={"face_threshold": strict})
    client.patch(f"/camera-groups/{group}/", headers=admin, json={"face_threshold": None})
    assert is_matched(T0 + timedelta(hours=3)) is False
    # Общий порог из настроек.
    client.patch(f"/watch-lists/{wl}/", headers=admin, json={"face_threshold": None})
    assert is_matched(T0 + timedelta(hours=4)) is True
    assert client.patch("/settings/", headers=admin, json={"face_confidence_threshold": strict}).status_code == 200
    assert is_matched(T0 + timedelta(hours=5)) is False


def test_inactive_list_card_and_validity_period(client, admin, people):
    wl, group = default_ids(client, admin)
    card = make_card(client, admin, "Временный пропуск", [wl], people["portraits"][0],
                     active_after="2026-01-10T00:00:00Z", active_before="2026-01-20T00:00:00Z")

    def is_matched(when):
        return get_event(client, admin, register(people, 0, when, group))["matched"]

    assert is_matched(T0) is True
    assert is_matched(datetime(2026, 1, 9, 23, 0)) is False   # пропуск ещё не действует
    assert is_matched(datetime(2026, 1, 20, 0, 0)) is False   # срок истёк
    client.patch(f"/cards/humans/{card}/", headers=admin, json={"active": False})
    assert is_matched(T0 + timedelta(hours=1)) is False
    client.patch(f"/cards/humans/{card}/", headers=admin, json={"active": True})
    client.patch(f"/watch-lists/{wl}/", headers=admin, json={"active": False})
    assert is_matched(T0 + timedelta(hours=2)) is False
    # Карточка в двух списках: достаточно одного активного.
    vip = client.post("/watch-lists/", headers=admin, json={"name": "VIP"}).json()["id"]
    client.patch(f"/cards/humans/{card}/", headers=admin, json={"watch_lists": [wl, vip]})
    event = get_event(client, admin, register(people, 0, T0 + timedelta(hours=3), group))
    assert event["matched"] is True and event["matched_lists"] == [vip]


def test_ignore_events_and_unmatched_switch(client, admin, people):
    wl, group = default_ids(client, admin)
    make_card(client, admin, "Сотрудник", [wl], people["portraits"][0])
    client.patch(f"/watch-lists/{wl}/", headers=admin, json={"ignore_events": True})
    assert register(people, 0, T0, group) is None          # список «не создавать события»
    assert register(people, 5, T0, group) is not None      # посторонний фиксируется
    client.patch("/settings/", headers=admin, json={"unmatched_events_enabled": False})
    assert register(people, 5, T0 + timedelta(hours=1), group) is None
    assert client.get("/events/faces/", headers=admin).json()["count"] == 1


def test_acknowledgement(client, admin, roles, people):
    wl, group = default_ids(client, admin)
    alarm = client.post("/watch-lists/", headers=admin, json={"name": "Нежелательные", "acknowledge": True, "notify": True}).json()["id"]
    make_card(client, admin, "Нарушитель", [alarm], people["portraits"][0])
    make_card(client, admin, "Сотрудник", [wl], people["portraits"][1])

    e1 = register(people, 0, T0, group)
    e2 = register(people, 0, T0 + timedelta(seconds=5), group)
    e3 = register(people, 1, T0, group)
    assert get_event(client, admin, e1)["acknowledged"] is False and get_event(client, admin, e3)["acknowledged"] is True
    pending = client.get("/events/faces/", headers=admin, params={"acknowledged": False}).json()
    assert pending["count"] == 2
    episode_id = get_event(client, admin, e1)["episode_id"]
    assert get_event(client, admin, e2)["episode_id"] == episode_id
    assert client.get(f"/episodes/humans/{episode_id}/", headers=admin).json()["acknowledged"] is False

    r = client.patch(f"/events/faces/{e1}/", headers=admin, json={"acknowledged": True, "acknowledged_reaction": "Вызвана охрана"})
    assert r.status_code == 200 and r.json()["acknowledged"] is True and r.json()["acknowledged_reaction"] == "Вызвана охрана"
    assert r.json()["acknowledged_by"] and r.json()["acknowledged_date"]
    assert client.get(f"/episodes/humans/{episode_id}/", headers=admin).json()["acknowledged"] is False  # e2 ещё не принято
    client.patch(f"/events/faces/{e2}/", headers=admin, json={"acknowledged": True})
    assert client.get(f"/episodes/humans/{episode_id}/", headers=admin).json()["acknowledged"] is True

    # Снятие принятия и принятие эпизода целиком.
    client.patch(f"/events/faces/{e1}/", headers=admin, json={"acknowledged": False})
    assert client.get("/episodes/humans/", headers=admin, params={"acknowledged": False}).json()["count"] == 1
    assert client.patch(f"/episodes/humans/{episode_id}/", headers=admin, json={"acknowledged": True}).json()["acknowledged"] is True
    assert client.get("/events/faces/", headers=admin, params={"acknowledged": False}).json()["count"] == 0

    # «Принять все» и права: роль «Пользователь» принимать может, роль без faceevent:change — нет.
    client.patch(f"/events/faces/{e1}/", headers=admin, json={"acknowledged": False})
    client.patch(f"/events/faces/{e2}/", headers=admin, json={"acknowledged": False})
    assert client.post("/events/faces/acknowledge/", headers=admin).json() == {"acknowledged": 2}
    assert client.get(f"/episodes/humans/{episode_id}/", headers=admin).json()["acknowledged"] is True
    rid = client.post("/groups/", headers=admin, json={"name": "Наблюдатель", "permissions": ["faceevent:view", "humanepisode:view"]}).json()["id"]
    client.post("/users/", headers=admin, json={"name": "watcher", "password": "watcher-pass-1", "role_id": rid})
    watcher = login(client, "watcher", "watcher-pass-1")
    assert client.get("/events/faces/", headers=watcher).status_code == 200
    assert client.patch(f"/events/faces/{e1}/", headers=watcher, json={"acknowledged": False}).status_code == 403
    assert client.patch(f"/episodes/humans/{episode_id}/", headers=watcher, json={"acknowledged": False}).status_code == 403
    assert client.post("/events/faces/acknowledge/", headers=watcher).status_code == 403

    # Непринятые события без совпадения — по общей настройке.
    client.patch("/settings/", headers=admin, json={"unmatched_acknowledge": True})
    assert get_event(client, admin, register(people, 5, T0, group))["acknowledged"] is False


def test_deduplication(client, admin, people):
    wl, group = default_ids(client, admin)
    make_card(client, admin, "Сотрудник", [wl], people["portraits"][0])
    client.patch(f"/camera-groups/{group}/", headers=admin, json={"deduplicate": True, "deduplicate_delay": 15})
    other_group = client.post("/camera-groups/", headers=admin, json={"name": "Склад", "deduplicate": True}).json()["id"]

    assert register(people, 0, T0, group) is not None
    assert register(people, 0, T0 + timedelta(seconds=10), group, variant="other") is None   # тот же человек, та же группа
    assert register(people, 1, T0 + timedelta(seconds=10), group) is not None                # другой человек
    assert register(people, 0, T0 + timedelta(seconds=10), other_group) is not None          # другая группа камер
    assert register(people, 0, T0 + timedelta(seconds=16), group) is not None                # интервал истёк
    # Для лиц без карточки дубли определяются по схожести.
    assert register(people, 5, T0, group) is not None
    assert register(people, 5, T0 + timedelta(seconds=5), group, variant="other") is None
    assert register(people, 4, T0 + timedelta(seconds=5), group) is not None


# --- Эпизоды ---
def test_episodes_grouping_and_closing(client, admin, people):
    wl, group = default_ids(client, admin)
    card = make_card(client, admin, "Сотрудник", [wl], people["portraits"][0])

    def episode_of(event_id):
        return get_event(client, admin, event_id)["episode_id"]

    a1 = register(people, 0, T0, group, quality=0.5)
    a2 = register(people, 0, T0 + timedelta(seconds=20), group, variant="other", quality=0.9)
    b1 = register(people, 5, T0 + timedelta(seconds=21), group)
    b2 = register(people, 5, T0 + timedelta(seconds=40), group, variant="other")
    assert episode_of(a1) == episode_of(a2) != episode_of(b1) == episode_of(b2)

    ep = client.get(f"/episodes/humans/{episode_of(a1)}/", headers=admin).json()
    assert ep["events_count"] == 2 and ep["matched"] is True and ep["matched_card_id"] == card
    assert ep["matched_card_name"] == "Сотрудник" and ep["best_event_id"] == a2   # лучшее качество
    assert ep["camera_groups"] == [group] and ep["matched_lists"] == [wl]
    assert ep["created_date"].startswith("2026-01-15T09:00:00") and ep["last_event_date"].startswith("2026-01-15T09:00:20")
    stranger = client.get(f"/episodes/humans/{episode_of(b1)}/", headers=admin).json()
    assert stranger["matched"] is False and stranger["events_count"] == 2

    # Пауза дольше 30 с — новый эпизод; непрерывное присутствие дольше 300 с — тоже.
    a3 = register(people, 0, T0 + timedelta(seconds=60), group)
    assert episode_of(a3) != episode_of(a1)
    last = None
    ids = set()
    for step in range(0, 340, 20):
        last = register(people, 0, T0 + timedelta(seconds=60 + step), group)
        ids.add(episode_of(last))
    assert len(ids) == 2

    # Все эпизоды, кроме последних, закрыты; список сам закрывает устаревшие.
    listing = client.get("/episodes/humans/", headers=admin).json()
    assert listing["count"] == 4 and all(e["open"] is False and e["closed_date"] for e in listing["results"])
    assert client.get("/episodes/humans/", headers=admin, params={"matched": False}).json()["count"] == 1
    assert client.get("/episodes/humans/", headers=admin, params={"matched_card": card}).json()["count"] == 3
    assert client.get("/episodes/humans/", headers=admin, params={"watch_lists": [wl]}).json()["count"] == 3
    assert client.get("/episodes/humans/", headers=admin, params={"camera_groups": [999]}).json()["count"] == 0
    in_episode = client.get("/events/faces/", headers=admin, params={"episode": episode_of(a1)}).json()
    assert sorted(e["id"] for e in in_episode["results"]) == [a1, a2]


def test_unmatched_episode_becomes_matched(client, admin, people):
    """Сначала человека нет в картотеке; после добавления карточки эпизод продолжается и получает совпадение."""
    wl, group = default_ids(client, admin)
    first = register(people, 0, T0, group)
    make_card(client, admin, "Новый сотрудник", [wl], people["portraits"][0])
    second = register(people, 0, T0 + timedelta(seconds=10), group, variant="other")
    e1, e2 = get_event(client, admin, first), get_event(client, admin, second)
    assert e1["matched"] is False and e2["matched"] is True and e1["episode_id"] == e2["episode_id"]
    assert client.get(f"/episodes/humans/{e1['episode_id']}/", headers=admin).json()["matched"] is True


# --- Фильтры и поиск ---
def test_event_filters_and_face_search(client, admin, people):
    wl, group = default_ids(client, admin)
    vip = client.post("/watch-lists/", headers=admin, json={"name": "VIP"}).json()["id"]
    hall = client.post("/camera-groups/", headers=admin, json={"name": "Холл"}).json()["id"]
    ivanov = make_card(client, admin, "Иванов Иван", [wl], people["portraits"][0])
    make_card(client, admin, "Петрова Анна", [vip], people["portraits"][1])
    ids = [
        register(people, 0, T0, group),
        register(people, 1, T0 + timedelta(minutes=1), hall),
        register(people, 2, T0 + timedelta(minutes=2), group),
        register(people, 0, T0 + timedelta(minutes=3), hall, variant="other"),
    ]

    def found(**params):
        return [e["id"] for e in client.get("/events/faces/", headers=admin, params=params).json()["results"]]

    assert found() == ids[::-1]                                   # новые первыми
    assert found(matched=True) == [ids[3], ids[1], ids[0]] and found(matched=False) == [ids[2]]
    assert found(watch_lists=[vip]) == [ids[1]] and found(watch_lists=[wl, vip]) == [ids[3], ids[1], ids[0]]
    assert found(camera_groups=[hall]) == [ids[3], ids[1]]
    assert found(matched_card=ivanov) == [ids[3], ids[0]]
    assert found(card_name_contains="петров") == [ids[1]]
    assert found(id_in=[ids[0], ids[2]]) == [ids[2], ids[0]]
    assert found(created_date_gte="2026-01-15T09:01:30", created_date_lte="2026-01-15T09:02:30") == [ids[2]]
    assert found(limit=2, offset=1) == [ids[2], ids[1]]

    # Поиск по лицу: по событию, по фото из карточки и с дополнительным фильтром.
    res = client.get("/events/faces/", headers=admin, params={"looks_like": f"faceevent:{ids[0]}"}).json()
    assert [e["id"] for e in res["results"]] == [ids[0], ids[3]]
    assert res["results"][0]["looks_like_confidence"] > res["results"][1]["looks_like_confidence"] > 0.7
    assert found(looks_like=f"faceevent:{ids[2]}") == [ids[2]]    # человека без карточки тоже можно искать
    assert found(looks_like=f"faceevent:{ids[0]}", camera_groups=[hall]) == [ids[3]]
    assert len(found(looks_like=f"faceevent:{ids[0]}", threshold=0)) == 4
    assert client.get("/events/faces/", headers=admin, params={"looks_like": "faceevent:99999"}).status_code == 404
    # Поиск карточки по событию и сравнение события с фото карточки.
    cards = client.get("/cards/humans/", headers=admin, params={"looks_like": f"faceevent:{ids[3]}"}).json()
    assert [c["id"] for c in cards["results"]] == [ivanov]
    face_id = get_event(client, admin, ids[3])["matched_face_id"]
    v = client.get("/verify/", headers=admin, params={"object1": f"faceevent:{ids[3]}", "object2": f"faceobject:{face_id}"}).json()
    assert v["match"] is True and v["confidence"] == pytest.approx(get_event(client, admin, ids[3])["confidence"], abs=1e-3)


def test_create_card_from_event(client, admin, people):
    wl, group = default_ids(client, admin)
    event_id = register(people, 3, T0, group)
    card = client.post("/cards/humans/", headers=admin, json={"name": "Из события", "watch_lists": [wl]}).json()
    r = client.post("/objects/faces/", headers=admin, data={"card": card["id"], "create_from": f"faceevent:{event_id}"})
    assert r.status_code == 201 and r.json()["has_source_photo"] is True
    assert client.get(f"/objects/faces/{r.json()['id']}/thumbnail/", headers=admin).status_code == 200
    again = get_event(client, admin, register(people, 3, T0 + timedelta(hours=1), group, variant="other"))
    assert again["matched"] is True and again["matched_card_id"] == card["id"]
    assert client.post("/objects/faces/", headers=admin, data={"card": card["id"], "create_from": "faceevent:99999"}).status_code == 404


# --- Настройки ---
def test_settings_api(client, admin, roles):
    current = client.get("/settings/", headers=admin).json()
    assert current == {
        "face_confidence_threshold": 0.7, "unmatched_events_enabled": True, "unmatched_acknowledge": False,
        "episode_event_timeout": 30, "episode_max_duration": 300, "liveness_threshold": 0.5,
        # dedup_window и unmatched_liveness_filter выключены в conftest (по умолчанию включены).
        "dedup_window": 0, "unmatched_liveness_filter": False, "unmatched_liveness_min": 0.35,
    }
    r = client.patch("/settings/", headers=admin, json={"episode_event_timeout": 60})
    assert r.json()["episode_event_timeout"] == 60 and r.json()["face_confidence_threshold"] == 0.7
    assert client.get("/settings/", headers=admin).json()["episode_event_timeout"] == 60
    assert client.patch("/settings/", headers=admin, json={"face_confidence_threshold": 1.5}).status_code == 422
    assert client.patch("/settings/", headers=admin, json={"episode_event_timeout": 0}).status_code == 422
    assert client.get("/settings/", headers=admin).json()["face_confidence_threshold"] == 0.7
    client.post("/users/", headers=admin, json={"name": "oper", "password": "oper-pass-123", "role_id": roles["Оператор"]["id"]})
    oper = login(client, "oper", "oper-pass-123")
    assert client.get("/settings/", headers=oper).status_code == 200
    assert client.patch("/settings/", headers=oper, json={"episode_event_timeout": 5}).status_code == 403
    assert ("update", "runtimesetting") in [(l["action"], l["object_type"]) for l in client.get("/auditlogs/", headers=admin).json()["results"]]


# --- Лента в реальном времени ---
def test_websocket_feed(client, admin, people):
    wl, group = default_ids(client, admin)
    make_card(client, admin, "Иванов", [wl], people["portraits"][0])
    token = admin["Authorization"].split()[1]
    with client.websocket_connect("/ws/events/") as ws:
        ws.send_json({"token": token})
        assert ws.receive_json() == {"type": "ready"}
        event_id = register(people, 0, T0, group, publish=True)
        message = ws.receive_json()
        assert message["type"] == "face_event_created" and message["data"]["id"] == event_id
        assert message["data"]["matched_card_name"] == "Иванов" and message["data"]["created_date"] == "2026-01-15T09:00:00"
        assert "embedding" not in message["data"]
        episode = ws.receive_json()
        assert episode["type"] == "human_episode_updated" and episode["data"]["events_count"] == 1

        client.patch(f"/events/faces/{event_id}/", headers=admin, json={"acknowledged": False})
        assert ws.receive_json()["type"] == "face_event_updated"
        assert ws.receive_json()["type"] == "human_episode_updated"


def test_websocket_requires_token(client, admin, roles):
    for hello in ({"token": "wrong"}, {"nope": 1}, ["x"]):
        with client.websocket_connect("/ws/events/") as ws:
            ws.send_json(hello)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401
    # Пользователь без права на события к ленте не допускается.
    rid = client.post("/groups/", headers=admin, json={"name": "Кадры", "permissions": ["humancard:view"]}).json()["id"]
    client.post("/users/", headers=admin, json={"name": "hr", "password": "hr-pass-12345", "role_id": rid})
    hr = login(client, "hr", "hr-pass-12345")["Authorization"].split()[1]
    with client.websocket_connect("/ws/events/") as ws:
        ws.send_json({"token": hr})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# --- Сквозная проверка на видео ---
def test_video_with_card_file(client, admin, people, video_file):
    wl, group = default_ids(client, admin)
    staff = [make_card(client, admin, f"Сотрудник {i}", [wl], people["portraits"][i]) for i in range(3)]
    token = admin["Authorization"].split()[1]
    with client.websocket_connect("/ws/events/") as ws:
        ws.send_json({"token": token})
        assert ws.receive_json() == {"type": "ready"}
        with open(video_file, "rb") as fh:
            vid = client.post("/videos/", headers=admin, data={"camera_group": group, "process": "true"},
                              files={"source_file": ("scene.mp4", fh, "video/mp4")}).json()["id"]
        done = wait_for_video(client, admin, vid, {"COMPLETED", "FAILED"})
        assert done["state"] == "COMPLETED" and done["events_created"] == 12
        live = [ws.receive_json() for _ in range(24)]          # по событию и обновлению эпизода на каждый трек
    assert sum(m["type"] == "face_event_created" for m in live) == 12

    all_events = client.get("/events/faces/", headers=admin, params={"video_archive": vid, "limit": 100}).json()["results"]
    matched = [e for e in all_events if e["matched"]]
    assert len(matched) == 6 and {e["matched_card_id"] for e in matched} == set(staff)
    assert all(e["confidence"] > 0.9 for e in matched)

    # Каждый человек прошёл дважды с паузой 2 с — это один эпизод из двух событий; после обработки все закрыты.
    eps = client.get("/episodes/humans/", headers=admin, params={"video_archive": vid}).json()
    assert eps["count"] == 6 and all(e["events_count"] == 2 and e["open"] is False for e in eps["results"])
    assert sum(e["matched"] for e in eps["results"]) == 3

    # Дедупликация в группе: при повторной обработке второй проход каждого человека не создаёт события.
    client.patch(f"/camera-groups/{group}/", headers=admin, json={"deduplicate": True, "deduplicate_delay": 15})
    client.post(f"/videos/{vid}/process/", headers=admin)
    assert wait_for_video(client, admin, vid, {"COMPLETED", "FAILED"})["events_created"] == 6
    assert client.get("/episodes/humans/", headers=admin).json()["count"] == 6

    client.delete(f"/videos/{vid}/", headers=admin)
    assert client.get("/events/faces/", headers=admin).json()["count"] == 0
    assert client.get("/episodes/humans/", headers=admin).json()["count"] == 0

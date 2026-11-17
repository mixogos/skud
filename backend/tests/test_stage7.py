import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import create_engine, inspect, text

from app import access, events, models
from app.db import SessionLocal
from app.recognition import engine as eng
from app.recognition import model_store
from app.video.processor import TrackResult
from app.webhooks import matches, sender
from conftest import GROUP_PHOTO, login

needs_models = pytest.mark.skipif(
    not (model_store.models_present() and GROUP_PHOTO.is_file()),
    reason="нет моделей или тестового фото",
)


def local_to_utc(year, month, day, hour, minute=0):
    """Момент по местному времени сервера → UTC без пояса (как хранит база)."""
    return datetime(year, month, day, hour, minute).astimezone().astimezone(timezone.utc).replace(tzinfo=None)


WEDNESDAY_NOON = local_to_utc(2026, 1, 14, 12)      # среда
WEDNESDAY_NIGHT = local_to_utc(2026, 1, 14, 23)
SUNDAY_NOON = local_to_utc(2026, 1, 18, 12)
WORKDAYS = {d: [["08:00", "20:00"]] for d in ("monday", "tuesday", "wednesday", "thursday", "friday")}


@pytest.fixture(scope="module")
def people():
    image = eng.decode_image(GROUP_PHOTO.read_bytes())
    faces = sorted(eng.get_engine().detect_and_extract(image), key=lambda f: f.bbox[0])
    h, w = image.shape[:2]
    portraits = []
    for f in faces:
        left, top, right, bottom = f.bbox
        mx, my = right - left, bottom - top
        portraits.append(eng.encode_jpeg(image[max(0, top - my):min(h, bottom + my), max(0, left - mx // 2):min(w, right + mx // 2)]))
    return {"image": image, "faces": faces, "portraits": portraits}


def make_card(client, admin, name, lists, portrait, **extra):
    card = client.post("/cards/humans/", headers=admin, json={"name": name, "watch_lists": lists, **extra}).json()
    r = client.post("/objects/faces/", headers=admin, data={"card": card["id"]}, files={"source_photo": ("p.jpg", portrait, "image/jpeg")})
    assert r.status_code == 201, r.text
    return card["id"]


def setup_access(client, admin):
    """Группа «Проходная» с контролем доступа; списки «Персонал» (разрешает) и «Чёрный список» (запрещает)."""
    group = client.get("/camera-groups/", headers=admin).json()["results"][0]["id"]
    assert client.patch(f"/camera-groups/{group}/", headers=admin, json={"access_control": True}).json()["access_control"] is True
    staff = client.post("/watch-lists/", headers=admin, json={"name": "Персонал", "access_policy": "allow", "access_schedule": WORKDAYS}).json()
    black = client.post("/watch-lists/", headers=admin, json={"name": "Чёрный список", "access_policy": "deny"}).json()
    assert staff["access_policy"] == "allow" and staff["access_schedule"] == WORKDAYS
    return group, staff["id"], black["id"]


def register(people, person, when, group):
    result = TrackResult(1, people["faces"][person], people["image"], 0.8, 0.0, 0.0, 1.0, 5)
    with SessionLocal() as db:
        reg = events.process_track(db, result, created_date=when, camera_group_id=group)
        db.commit()
        return (reg.event.id if reg.event else None), reg.access, reg.access_reason


# --- Расписания (без нейросетей) ---
def test_schedule_validation_and_matching():
    assert access.validate_schedule({}) == {}
    assert access.validate_schedule({"monday": [["08:00", "12:00"], ["13:00", "24:00"]], "sunday": []}) == {
        "monday": [["08:00", "12:00"], ["13:00", "24:00"]]
    }
    for bad in ({"funday": [["08:00", "09:00"]]}, {"monday": [["9:00", "10:00"]]}, {"monday": [["10:00", "09:00"]]},
                {"monday": [["08:00"]]}, {"monday": "08:00-09:00"}, {"monday": [["08:00", "25:00"]]}):
        with pytest.raises(ValueError):
            access.validate_schedule(bad)

    wednesday = datetime(2026, 1, 14, 12, 0)
    assert access.in_schedule({}, wednesday) is True and access.in_schedule(None, wednesday) is True
    assert access.in_schedule(WORKDAYS, wednesday) is True
    assert access.in_schedule(WORKDAYS, wednesday.replace(hour=7, minute=59)) is False
    assert access.in_schedule(WORKDAYS, wednesday.replace(hour=8)) is True
    assert access.in_schedule(WORKDAYS, wednesday.replace(hour=20)) is False        # конец интервала не включается
    assert access.in_schedule(WORKDAYS, datetime(2026, 1, 18, 12, 0)) is False       # воскресенье
    assert access.in_schedule({"sunday": [["00:00", "24:00"]]}, datetime(2026, 1, 18, 23, 59)) is True


def test_schedule_api_validation(client, admin):
    wl = client.get("/watch-lists/", headers=admin).json()["results"][0]["id"]
    assert client.patch(f"/watch-lists/{wl}/", headers=admin, json={"access_schedule": {"monday": [["20:00", "08:00"]]}}).status_code == 422
    assert client.patch(f"/watch-lists/{wl}/", headers=admin, json={"access_policy": "maybe"}).status_code == 422
    assert client.post("/cards/humans/", headers=admin, json={"name": "A", "watch_lists": [wl], "access_schedule": {"xday": []}}).status_code == 422
    card = client.post("/cards/humans/", headers=admin, json={"name": "A", "watch_lists": [wl], "access_schedule": WORKDAYS}).json()
    assert card["access_schedule"] == WORKDAYS
    assert client.patch(f"/cards/humans/{card['id']}/", headers=admin, json={"access_schedule": {}}).json()["access_schedule"] == {}


def test_database_upgrade_adds_columns(tmp_path, monkeypatch):
    """Старая база без новых колонок обновляется при запуске, данные сохраняются."""
    from app import bootstrap

    old = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE watch_lists (id INTEGER PRIMARY KEY, name VARCHAR(256), active BOOLEAN)"))
        conn.execute(text("INSERT INTO watch_lists (id, name, active) VALUES (1, 'Старый список', 1)"))
    monkeypatch.setattr(bootstrap, "engine", old)
    added = bootstrap.add_missing_columns()
    assert "watch_lists.access_policy" in added and "watch_lists.access_schedule" in added
    assert {"access_policy", "access_schedule", "color", "face_threshold"} <= {c["name"] for c in inspect(old).get_columns("watch_lists")}
    with old.connect() as conn:
        row = conn.execute(text("SELECT name, access_policy, access_schedule, color, face_threshold FROM watch_lists")).one()
    assert row == ("Старый список", "none", "{}", "#3d7eff", None)
    assert bootstrap.add_missing_columns() == []
    old.dispose()


# --- Решение о доступе ---
@needs_models
def test_access_decisions(client, admin, people):
    group, staff, black = setup_access(client, admin)
    employee = make_card(client, admin, "Сотрудник", [staff], people["portraits"][0])
    make_card(client, admin, "Нарушитель", [black], people["portraits"][1])
    make_card(client, admin, "Уволенный", [staff, black], people["portraits"][2])
    default_list = client.get("/watch-lists/", headers=admin).json()["results"][0]["id"]
    make_card(client, admin, "Гость", [default_list], people["portraits"][3])

    def decision(person, when):
        _, result, reason = register(people, person, when, group)
        return result, reason

    assert decision(0, WEDNESDAY_NOON) == ("granted", "Список «Персонал»")
    assert decision(0, WEDNESDAY_NIGHT) == ("denied", "Вне расписания")
    assert decision(0, SUNDAY_NOON) == ("denied", "Вне расписания")
    assert decision(1, WEDNESDAY_NOON) == ("denied", "Список «Чёрный список» запрещает проход")
    assert decision(2, WEDNESDAY_NOON)[0] == "denied"                       # запрет сильнее разрешения
    assert decision(3, WEDNESDAY_NOON) == ("denied", "Нет списка, разрешающего проход")
    assert decision(5, WEDNESDAY_NOON) == ("denied", "Человека нет в картотеке")

    # Личное расписание заменяет расписание списка — в обе стороны.
    client.patch(f"/cards/humans/{employee}/", headers=admin, json={"access_schedule": {"sunday": [["10:00", "14:00"]]}})
    assert decision(0, SUNDAY_NOON) == ("granted", "Личное расписание")
    assert decision(0, WEDNESDAY_NOON + timedelta(minutes=1)) == ("denied", "Вне личного расписания")
    client.patch(f"/cards/humans/{employee}/", headers=admin, json={"access_schedule": {}})

    # Истёкший пропуск и отключённая карточка — отказ: человек перестаёт находиться в картотеке.
    client.patch(f"/cards/humans/{employee}/", headers=admin, json={"active_before": "2026-01-01T00:00:00Z"})
    assert decision(0, WEDNESDAY_NOON + timedelta(minutes=2)) == ("denied", "Человека нет в картотеке")
    client.patch(f"/cards/humans/{employee}/", headers=admin, json={"active_before": None})

    # Решение записано в событии и доступно в фильтре.
    granted = client.get("/events/faces/", headers=admin, params={"access": "granted"}).json()
    assert granted["count"] == 2 and all(e["matched_card_name"] == "Сотрудник" for e in granted["results"])
    assert granted["results"][0]["access_reason"]
    assert client.get("/events/faces/", headers=admin, params={"access": "denied"}).json()["count"] == 8

    # В группе без контроля доступа решение не принимается.
    other = client.post("/camera-groups/", headers=admin, json={"name": "Коридор"}).json()["id"]
    event_id, result, _ = register(people, 0, WEDNESDAY_NOON, other)
    assert result is None and client.get(f"/events/faces/{event_id}/", headers=admin).json()["access"] is None


# --- Внешние детекторы ---
@needs_models
def test_external_detector_flow(client, admin, roles, people):
    group, staff, black = setup_access(client, admin)
    client.patch(f"/watch-lists/{staff}/", headers=admin, json={"access_schedule": {}})   # круглосуточно
    card = make_card(client, admin, "Сотрудник", [staff], people["portraits"][0])

    r = client.post("/cameras/", headers=admin, json={"name": "Терминал у входа", "group_id": group})
    assert r.status_code == 201
    detector = r.json()
    token = detector["token"]
    assert len(token) == 64 and detector["external_detector"] is True
    listed = client.get("/cameras/", headers=admin).json()["results"]
    assert len(listed) == 1 and listed[0]["token"] is None            # токен больше нигде не показывается
    with SessionLocal() as db:
        assert db.query(models.Camera).one().token_hash != token
    assert client.post("/cameras/", headers=admin, json={"name": "X", "group_id": 999}).status_code == 400
    device = {"Authorization": f"Token {token}"}

    def send(frame, headers=device, **data):
        return client.post("/events/faces/add/", headers=headers, data=data, files={"fullframe": ("f.jpg", frame, "image/jpeg")})

    # Сотрудник: совпадение и разрешение, событие привязано к детектору.
    r = send(people["portraits"][0])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["matched"] is True and out["card"] == {"id": card, "name": "Сотрудник"} and out["access"] == "granted"
    assert out["confidence"] > 0.9 and out["watch_lists"] == [staff] and out["quality"] > 0
    event = client.get(f"/events/faces/{out['event']}/", headers=admin).json()
    assert event["camera_id"] == detector["id"] and event["camera_group_id"] == group and event["access"] == "granted"
    assert client.get("/events/faces/", headers=admin, params={"cameras": [detector["id"]]}).json()["count"] == 1

    # Посторонний и кадр без лица — отказ; без лица событие не создаётся.
    stranger = send(people["portraits"][5]).json()
    assert stranger["matched"] is False and stranger["access"] == "denied" and stranger["event"]
    blank = send(eng.encode_jpeg(__import__("numpy").full((300, 300, 3), 120, "uint8"))).json()
    assert blank == {"event": None, "matched": False, "card": None, "confidence": None, "watch_lists": [],
                     "access": "denied", "reason": "Лицо не найдено", "quality": None, "liveness": None}
    assert send(b"not an image").status_code == 400

    # Время кадра от устройства учитывается в расписании.
    client.patch(f"/watch-lists/{staff}/", headers=admin, json={"access_schedule": WORKDAYS})
    night = datetime(2026, 1, 14, 23, 0).astimezone().isoformat()
    assert send(people["portraits"][0], timestamp=night).json()["access"] == "denied"
    noon = datetime(2026, 1, 14, 12, 0).astimezone().isoformat()
    assert send(people["portraits"][0], timestamp=noon).json()["access"] == "granted"

    # Дедупликация не мешает решению: событие не создаётся, но проход разрешён.
    client.patch(f"/camera-groups/{group}/", headers=admin, json={"deduplicate": True, "deduplicate_delay": 60})
    again = send(people["portraits"][0], timestamp=datetime(2026, 1, 14, 12, 0, 20).astimezone().isoformat()).json()
    assert again["event"] is None and again["access"] == "granted" and again["matched"] is True

    # Авторизация: токен пользователя не подходит устройству, и наоборот.
    assert send(people["portraits"][0], headers={}).status_code == 401
    assert send(people["portraits"][0], headers=admin).status_code == 401
    assert send(people["portraits"][0], headers={"Authorization": "Token " + "0" * 64}).status_code == 401
    assert client.get("/users/me/", headers=device).status_code == 401
    assert client.get("/events/faces/", headers=device).status_code == 401

    # Замена токена и отключение детектора.
    new_token = client.post(f"/cameras/{detector['id']}/token/", headers=admin).json()["token"]
    assert new_token != token and send(people["portraits"][0]).status_code == 401
    device2 = {"Authorization": f"Token {new_token}"}
    assert send(people["portraits"][0], headers=device2).status_code == 200
    client.patch(f"/cameras/{detector['id']}/", headers=admin, json={"active": False})
    assert send(people["portraits"][0], headers=device2).status_code == 401

    # Права и удаление: события остаются, ссылка на детектор обнуляется.
    client.post("/users/", headers=admin, json={"name": "viewer", "password": "viewer-pass-1", "role_id": roles["Пользователь"]["id"]})
    viewer = login(client, "viewer", "viewer-pass-1")
    assert client.get("/cameras/", headers=viewer).status_code == 200
    assert client.post("/cameras/", headers=viewer, json={"name": "X", "group_id": group}).status_code == 403
    assert client.post(f"/cameras/{detector['id']}/token/", headers=viewer).status_code == 403
    assert client.delete(f"/cameras/{detector['id']}/", headers=admin).status_code == 204
    assert client.get(f"/events/faces/{out['event']}/", headers=admin).json()["camera_id"] is None


# --- Вебхуки ---
class Receiver:
    """Локальный HTTP-сервер, записывающий полученные вебхуки. fail_first — сколько запросов отклонить."""

    def __init__(self, fail_first=0):
        self.requests, self.fail = [], fail_first
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if outer.fail > 0:
                    outer.fail -= 1
                    self.send_response(500)
                else:
                    outer.requests.append((dict(self.headers), body))
                    self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def receiver():
    r = Receiver()
    yield r
    r.close()


def test_webhook_filter_matching():
    event = {"matched": True, "matched_lists": [2, 5], "camera_group_id": 1, "camera_id": 7, "access": "granted"}
    assert matches({}, event) and matches(None, event)
    assert matches({"matched": True}, event) and not matches({"matched": False}, event)
    assert matches({"watch_lists": [5, 9]}, event) and not matches({"watch_lists": [9]}, event)
    assert matches({"camera_groups": [1]}, event) and not matches({"camera_groups": [2]}, event)
    assert matches({"cameras": [7]}, event) and not matches({"cameras": [8]}, event)
    assert matches({"access": "granted"}, event) and not matches({"access": "denied"}, event)
    assert not matches({"matched": True, "access": "denied"}, event)


def test_webhook_crud_and_validation(client, admin, roles, receiver):
    assert client.post("/webhooks/", headers=admin, json={"name": "X", "url": "ftp://host/x"}).status_code == 422
    assert client.post("/webhooks/", headers=admin, json={"name": "X", "url": "http://user:pass@host/x"}).status_code == 422
    assert client.post("/webhooks/", headers=admin, json={"name": "X", "url": receiver.url, "filters": {"access": "maybe"}}).status_code == 422
    r = client.post("/webhooks/", headers=admin, json={"name": "Дверь", "url": receiver.url, "secret": "s3cret-key", "filters": {"access": "granted"}})
    assert r.status_code == 201
    hook = r.json()
    assert hook["has_secret"] is True and "secret" not in hook and hook["filters"]["access"] == "granted"
    assert "s3cret-key" not in client.get("/webhooks/", headers=admin).text

    # Пробная отправка: подпись проверяется секретом.
    assert client.post(f"/webhooks/{hook['id']}/test/", headers=admin).json() == {"ok": True, "error": ""}
    headers, body = receiver.requests[-1]
    assert headers["X-Skud-Event"] == "test"
    assert headers["X-Skud-Signature"] == "sha256=" + hmac.new(b"s3cret-key", body, hashlib.sha256).hexdigest()
    assert client.get("/webhooks/", headers=admin).json()["results"][0]["last_status"] == "ok"

    client.patch(f"/webhooks/{hook['id']}/", headers=admin, json={"url": "http://127.0.0.1:9/none", "secret": ""})
    result = client.post(f"/webhooks/{hook['id']}/test/", headers=admin).json()
    assert result["ok"] is False and result["error"]
    listed = client.get("/webhooks/", headers=admin).json()["results"][0]
    assert listed["last_status"] == "error" and listed["has_secret"] is False
    assert "s3cret-key" not in client.get("/auditlogs/", headers=admin).text

    client.post("/users/", headers=admin, json={"name": "oper", "password": "oper-pass-123", "role_id": roles["Пользователь"]["id"]})
    oper = login(client, "oper", "oper-pass-123")
    assert client.get("/webhooks/", headers=oper).status_code == 200
    assert client.post("/webhooks/", headers=oper, json={"name": "X", "url": receiver.url}).status_code == 403
    assert client.delete(f"/webhooks/{hook['id']}/", headers=admin).status_code == 204


@needs_models
def test_webhook_delivery_on_events(client, admin, people, receiver):
    group, staff, black = setup_access(client, admin)
    client.patch(f"/watch-lists/{staff}/", headers=admin, json={"access_schedule": {}})
    make_card(client, admin, "Сотрудник", [staff], people["portraits"][0])
    all_hook = client.post("/webhooks/", headers=admin, json={"name": "Все события", "url": receiver.url + "?all"}).json()
    client.post("/webhooks/", headers=admin, json={"name": "Открыть дверь", "url": receiver.url + "?door", "filters": {"access": "granted", "camera_groups": [group]}})
    client.post("/webhooks/", headers=admin, json={"name": "Выключен", "url": receiver.url + "?off", "active": False})
    token = client.post("/cameras/", headers=admin, json={"name": "Терминал", "group_id": group}).json()["token"]
    device = {"Authorization": f"Token {token}"}

    def send(person):
        return client.post("/events/faces/add/", headers=device, files={"fullframe": ("f.jpg", people["portraits"][person], "image/jpeg")}).json()

    granted = send(0)
    send(5)   # посторонний: отказ
    assert sender.wait_idle()
    bodies = [json.loads(body) for _, body in receiver.requests]
    assert len(bodies) == 3                                    # «все события» ×2 и «открыть дверь» ×1
    door = [b[0] for b in bodies if b[0]["access"] == "granted"]
    assert len(door) == 2 and all(e["id"] == granted["event"] and e["matched_card_name"] == "Сотрудник" for e in door)
    assert receiver.requests[0][0]["X-Skud-Event"] == "face_event_created" and "X-Skud-Signature" not in receiver.requests[0][0]
    assert "embedding" not in bodies[0][0]
    assert client.get("/webhooks/", headers=admin).json()["results"][0]["last_status"] == "ok"
    assert all_hook["last_status"] == ""


def test_webhook_retries(client, admin, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "WEBHOOK_RETRY_DELAY", 0.01)
    flaky = Receiver(fail_first=2)
    try:
        hook = client.post("/webhooks/", headers=admin, json={"name": "Нестабильный", "url": flaky.url, "send_attempts": 3}).json()
        sender.enqueue(hook["id"], "face_event_created", [{"id": 1}])
        assert sender.wait_idle()
        assert len(flaky.requests) == 1                        # третья попытка удалась
        assert client.get("/webhooks/", headers=admin).json()["results"][0]["last_status"] == "ok"

        flaky.fail = 5
        sender.enqueue(hook["id"], "face_event_created", [{"id": 2}])
        assert sender.wait_idle()
        listed = client.get("/webhooks/", headers=admin).json()["results"][0]
        assert listed["last_status"] == "error" and "500" in listed["last_error"] and flaky.fail == 2   # ровно три попытки
    finally:
        flaky.close()


# --- Отчёт о рабочем времени ---
@needs_models
def test_worktime_report(client, admin, roles, people):
    group, staff, black = setup_access(client, admin)
    anna = make_card(client, admin, "Анна", [staff], people["portraits"][0])
    boris = make_card(client, admin, "Борис", [staff], people["portraits"][1])
    for person, hour, minute in ((0, 9, 2), (0, 13, 30), (0, 18, 15), (1, 10, 0), (5, 11, 0)):
        register(people, person, local_to_utc(2026, 1, 14, hour, minute), group)
    register(people, 0, local_to_utc(2026, 1, 15, 8, 55), group)
    register(people, 1, local_to_utc(2026, 1, 20, 9, 0), group)     # вне периода

    params = {"date_from": "2026-01-14", "date_to": "2026-01-15"}
    report = client.get("/reports/worktime/", headers=admin, params=params).json()
    assert report["count"] == 3
    assert report["results"][0] == {"card_id": anna, "card_name": "Анна", "date": "2026-01-14", "first_seen": "09:02:00",
                                    "last_seen": "18:15:00", "duration_seconds": 9 * 3600 + 13 * 60, "events": 3}
    assert report["results"][1]["card_name"] == "Борис" and report["results"][1]["duration_seconds"] == 0
    assert report["results"][2]["date"] == "2026-01-15" and report["results"][2]["first_seen"] == "08:55:00"
    assert client.get("/reports/worktime/", headers=admin, params={**params, "card": boris}).json()["count"] == 1
    assert client.get("/reports/worktime/", headers=admin, params={**params, "watch_lists": [black]}).json()["count"] == 0
    assert client.get("/reports/worktime/", headers=admin, params={**params, "camera_groups": [group]}).json()["count"] == 3

    csv_response = client.get("/reports/worktime/", headers=admin, params={**params, "format": "csv"})
    assert csv_response.headers["content-type"].startswith("text/csv") and "worktime_2026-01-14_2026-01-15.csv" in csv_response.headers["content-disposition"]
    lines = csv_response.content.decode("utf-8-sig").strip().split("\r\n")
    assert lines[0] == "Дата;Сотрудник;Приход;Уход;Время (ч:мм);Событий" and lines[1] == "2026-01-14;Анна;09:02:00;18:15:00;9:13;3"
    assert csv_response.content.startswith(b"\xef\xbb\xbf")

    assert client.get("/reports/worktime/", headers=admin, params={"date_from": "2026-02-01", "date_to": "2026-01-01"}).status_code == 400
    assert client.get("/reports/worktime/", headers=admin, params={"date_from": "2026-01-01", "date_to": "2026-12-31"}).status_code == 400
    rid = client.post("/groups/", headers=admin, json={"name": "Охрана", "permissions": ["faceevent:view"]}).json()["id"]
    client.post("/users/", headers=admin, json={"name": "guard", "password": "guard-pass-12", "role_id": rid})
    assert client.get("/reports/worktime/", headers=login(client, "guard", "guard-pass-12"), params=params).status_code == 403

from pathlib import Path

import cv2
import numpy as np
import pytest

from app import storage
from app.recognition import engine as eng
from app.recognition import model_store
from conftest import login

GROUP_PHOTO = Path(__file__).parent / "data" / "t1.jpg"

needs_models = pytest.mark.skipif(
    not (model_store.models_present() and GROUP_PHOTO.is_file()),
    reason="нет моделей или тестового фото",
)


@pytest.fixture(scope="module")
def portraits():
    """Шесть портретов (JPEG), вырезанных из группового фото, и искажённая версия всего фото."""
    image = eng.decode_image(GROUP_PHOTO.read_bytes())
    faces = eng.get_engine().detect(image)
    faces.sort(key=lambda f: f.bbox[0])
    crops = []
    h, w = image.shape[:2]
    for f in faces:
        left, top, right, bottom = f.bbox
        mx, my = (right - left), (bottom - top)
        crop = image[max(0, top - my):min(h, bottom + my), max(0, left - mx // 2):min(w, right + mx // 2)]
        crops.append(eng.encode_jpeg(crop))
    rot = cv2.getRotationMatrix2D((w / 2, h / 2), 6, 0.85)
    distorted = cv2.GaussianBlur(cv2.convertScaleAbs(cv2.warpAffine(image, rot, (w, h)), alpha=0.8, beta=10), (3, 3), 0)
    return crops, eng.encode_jpeg(distorted)


def default_list(client, admin):
    return client.get("/watch-lists/", headers=admin).json()["results"][0]["id"]


def make_card(client, admin, name, lists):
    r = client.post("/cards/humans/", headers=admin, json={"name": name, "watch_lists": lists})
    assert r.status_code == 201, r.text
    return r.json()


def upload_face(client, admin, card_id, jpeg, **form):
    return client.post(
        "/objects/faces/", headers=admin, data={"card": card_id, **form},
        files={"source_photo": ("p.jpg", jpeg, "image/jpeg")},
    )


# --- Списки наблюдения ---
def test_watch_list_crud(client, admin):
    lists = client.get("/watch-lists/", headers=admin).json()["results"]
    assert len(lists) == 1 and lists[0]["predefined"] is True
    r = client.post("/watch-lists/", headers=admin, json={"name": "Персонал", "color": "#00aa55", "face_threshold": 0.75})
    assert r.status_code == 201
    wl = r.json()
    assert wl["face_threshold"] == 0.75 and wl["active"] is True
    assert client.post("/watch-lists/", headers=admin, json={"name": "Персонал"}).status_code == 409
    assert client.post("/watch-lists/", headers=admin, json={"name": "X", "color": "red"}).status_code == 422
    assert client.post("/watch-lists/", headers=admin, json={"name": "X", "face_threshold": 1.5}).status_code == 422
    r = client.patch(f"/watch-lists/{wl['id']}/", headers=admin, json={"face_threshold": None, "acknowledge": True})
    assert r.json()["face_threshold"] is None and r.json()["acknowledge"] is True
    assert client.delete(f"/watch-lists/{lists[0]['id']}/", headers=admin).status_code == 400
    assert client.delete(f"/watch-lists/{wl['id']}/", headers=admin).status_code == 204


def test_watch_list_with_cards_cannot_be_deleted(client, admin):
    wl = client.post("/watch-lists/", headers=admin, json={"name": "VIP"}).json()
    card = make_card(client, admin, "Гость", [wl["id"]])
    assert client.delete(f"/watch-lists/{wl['id']}/", headers=admin).status_code == 409
    client.delete(f"/cards/humans/{card['id']}/", headers=admin)
    assert client.delete(f"/watch-lists/{wl['id']}/", headers=admin).status_code == 204


# --- Карточки ---
def test_card_crud_and_filters(client, admin):
    base = default_list(client, admin)
    vip = client.post("/watch-lists/", headers=admin, json={"name": "VIP"}).json()["id"]
    assert client.post("/cards/humans/", headers=admin, json={"name": "A", "watch_lists": []}).status_code == 422
    assert client.post("/cards/humans/", headers=admin, json={"name": "A", "watch_lists": [999]}).status_code == 400
    a = make_card(client, admin, "Иванов Иван", [base])
    b = make_card(client, admin, "Петров Пётр", [base, vip])
    assert a["filled"] is False and a["main_face"] is None and b["watch_lists"] == sorted([base, vip])

    def names(**params):
        return [c["name"] for c in client.get("/cards/humans/", headers=admin, params=params).json()["results"]]

    assert names() == ["Петров Пётр", "Иванов Иван"]
    assert names(name_contains="иван") == ["Иванов Иван"]
    assert names(watch_lists=[vip]) == ["Петров Пётр"]
    assert names(filled=True) == []

    r = client.patch(
        f"/cards/humans/{a['id']}/", headers=admin,
        json={"active": False, "meta": {"отдел": "Склад"}, "active_before": "2030-01-01T00:00:00Z"},
    )
    assert r.status_code == 200 and r.json()["meta"] == {"отдел": "Склад"} and r.json()["active"] is False
    assert names(active=True) == ["Петров Пётр"]
    bad = client.patch(f"/cards/humans/{a['id']}/", headers=admin, json={"active_after": "2031-01-01T00:00:00Z"})
    assert bad.status_code == 400
    assert client.patch(f"/cards/humans/{a['id']}/", headers=admin, json={"active_before": None}).json()["active_before"] is None

    assert client.delete(f"/cards/humans/{a['id']}/", headers=admin).status_code == 204
    assert client.get(f"/cards/humans/{a['id']}/", headers=admin).status_code == 404


def test_card_permissions(client, admin, roles):
    base = default_list(client, admin)
    card = make_card(client, admin, "Иванов", [base])
    client.post("/users/", headers=admin, json={"name": "viewer", "password": "viewer-pass-1", "role_id": roles["Пользователь"]["id"]})
    viewer = login(client, "viewer", "viewer-pass-1")
    assert client.get("/cards/humans/", headers=viewer).status_code == 200
    assert client.post("/cards/humans/", headers=viewer, json={"name": "X", "watch_lists": [base]}).status_code == 403
    assert client.delete(f"/cards/humans/{card['id']}/", headers=viewer).status_code == 403
    assert client.post("/objects/faces/", headers=viewer, data={"card": card["id"]}).status_code == 403
    assert client.post("/watch-lists/", headers=viewer, json={"name": "X"}).status_code == 403
    assert client.get("/cards/humans/").status_code == 401


# --- Фото лиц и поиск ---
@needs_models
def test_face_objects_and_search(client, admin, portraits):
    crops, distorted = portraits
    base = default_list(client, admin)
    cards = [make_card(client, admin, f"Человек {i}", [base]) for i in range(len(crops))]
    for card, jpeg in zip(cards, crops):
        r = upload_face(client, admin, card["id"], jpeg)
        assert r.status_code == 201, r.text
        face = r.json()
        assert face["has_source_photo"] is True and len(face["bbox"]) == 4

    card0 = client.get(f"/cards/humans/{cards[0]['id']}/", headers=admin).json()
    assert card0["filled"] is True and card0["face_objects"] == 1
    fid = card0["main_face"]
    thumb = client.get(f"/objects/faces/{fid}/thumbnail/", headers=admin)
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg" and thumb.content[:3] == b"\xff\xd8\xff"
    assert client.get(f"/objects/faces/{fid}/source_photo/", headers=admin).status_code == 200
    assert client.get(f"/objects/faces/{fid}/thumbnail/").status_code == 401
    assert client.get("/objects/faces/", headers=admin, params={"card": cards[0]["id"]}).json()["count"] == 1

    # Ищем каждого по искажённому групповому фото: первым должен найтись именно он.
    found = client.post("/detect/", headers=admin, files={"photo": ("d.jpg", distorted, "image/jpeg")}).json()["objects"]["face"]
    assert len(found) == len(crops)
    matched = set()
    for det in found:
        res = client.get("/cards/humans/", headers=admin, params={"looks_like": det["id"]}).json()
        assert res["count"] == 1, res
        assert res["results"][0]["looks_like_confidence"] >= 0.7
        matched.add(res["results"][0]["id"])
    assert matched == {c["id"] for c in cards}

    # Низкий порог возвращает всех, отсортированных по схожести; фильтры совмещаются с поиском.
    res = client.get("/cards/humans/", headers=admin, params={"looks_like": found[0]["id"], "threshold": 0}).json()
    confs = [c["looks_like_confidence"] for c in res["results"]]
    assert res["count"] == len(crops) and confs == sorted(confs, reverse=True)
    res = client.get("/cards/humans/", headers=admin, params={"looks_like": found[0]["id"], "threshold": 0, "name_contains": "Человек 3"}).json()
    assert res["count"] == 1

    # Сравнение с фото из карточки и поиск по фото из карточки.
    v = client.get("/verify/", headers=admin, params={"object1": f"faceobject:{fid}", "object2": f"faceobject:{fid}"}).json()
    assert v["match"] is True
    res = client.get("/cards/humans/", headers=admin, params={"looks_like": f"faceobject:{fid}"}).json()
    assert [c["id"] for c in res["results"]] == [cards[0]["id"]]
    assert client.get("/cards/humans/", headers=admin, params={"looks_like": "faceobject:99999"}).status_code == 404

    # Удаление фото и карточки убирает их из поиска и с диска.
    assert client.delete(f"/objects/faces/{fid}/", headers=admin).status_code == 204
    assert storage.face_file(fid, "thumb") is None and storage.face_file(fid, "src") is None
    assert client.get("/cards/humans/", headers=admin, params={"looks_like": f"detection:{found[0]['id'][10:]}", "threshold": 0}).json()["count"] == len(crops) - 1
    other = client.get(f"/cards/humans/{cards[1]['id']}/", headers=admin).json()["main_face"]
    assert client.delete(f"/cards/humans/{cards[1]['id']}/", headers=admin).status_code == 204
    assert storage.face_file(other, "thumb") is None
    assert client.get("/cards/humans/", headers=admin, params={"looks_like": found[0]["id"], "threshold": 0}).json()["count"] == len(crops) - 2


@needs_models
def test_face_upload_edge_cases(client, admin, portraits):
    crops, _ = portraits
    base = default_list(client, admin)
    card = make_card(client, admin, "Группа", [base])
    group = GROUP_PHOTO.read_bytes()

    r = upload_face(client, admin, card["id"], group)
    assert r.status_code == 400 and len(r.json()["detail"]["faces"]) == 6
    chosen = r.json()["detail"]["faces"][2]["id"]
    r = upload_face(client, admin, card["id"], group, create_from=chosen)
    assert r.status_code == 201 and r.json()["has_source_photo"] is True
    # Без исходного фото — только по результату /detect/.
    r = client.post("/objects/faces/", headers=admin, data={"card": card["id"], "create_from": chosen})
    assert r.status_code == 201 and r.json()["has_source_photo"] is False
    assert client.get(f"/objects/faces/{r.json()['id']}/thumbnail/", headers=admin).status_code == 200
    assert client.get(f"/objects/faces/{r.json()['id']}/source_photo/", headers=admin).status_code == 404

    blank = eng.encode_jpeg(np.full((200, 200, 3), 127, np.uint8))
    assert upload_face(client, admin, card["id"], blank).status_code == 400
    assert upload_face(client, admin, card["id"], b"not an image").status_code == 400
    assert upload_face(client, admin, 99999, crops[0]).status_code == 404
    assert client.post("/objects/faces/", headers=admin, data={"card": card["id"]}).status_code == 400
    assert client.post("/objects/faces/", headers=admin, data={"card": card["id"], "create_from": "detection:nope"}).status_code == 404


@needs_models
def test_batch_upload(client, admin, portraits):
    crops, _ = portraits
    base = default_list(client, admin)
    files = [("photos", (f"Сотрудник {i}.jpg", jpeg, "image/jpeg")) for i, jpeg in enumerate(crops[:3])]
    files.append(("photos", ("group.jpg", GROUP_PHOTO.read_bytes(), "image/jpeg")))
    files.append(("photos", ("broken.jpg", b"garbage", "image/jpeg")))
    r = client.post("/cards/humans/batch/", headers=admin, files=files, data={"watch_lists": [base], "name_prefix": "Офис: "})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 3 and out["failed"] == 2
    by_file = {i["file"]: i for i in out["results"]}
    assert "несколько лиц" in by_file["group.jpg"]["error"] and by_file["broken.jpg"]["status"] == "error"
    cards = client.get("/cards/humans/", headers=admin).json()["results"]
    assert sorted(c["name"] for c in cards) == [f"Офис: Сотрудник {i}" for i in range(3)]
    assert all(c["filled"] and c["watch_lists"] == [base] for c in cards)

    # Режим «самое крупное лицо» принимает групповое фото.
    r = client.post(
        "/cards/humans/batch/", headers=admin, data={"watch_lists": [base], "on_multiple": "biggest"},
        files=[("photos", ("group.jpg", GROUP_PHOTO.read_bytes(), "image/jpeg"))],
    )
    assert r.json()["created"] == 1
    assert client.post("/cards/humans/batch/", headers=admin, files=files, data={"watch_lists": [999]}).status_code == 400

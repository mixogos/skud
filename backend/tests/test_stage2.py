from pathlib import Path

import cv2
import numpy as np
import pytest

from app.recognition import engine as eng
from app.recognition import geometry, model_store
from app.recognition.cache import DetectionCache

DATA = Path(__file__).parent / "data"
GROUP_PHOTO = DATA / "t1.jpg"  # шесть человек

needs_models = pytest.mark.skipif(
    not (model_store.models_present() and GROUP_PHOTO.is_file()),
    reason="нет моделей или тестового фото",
)


def load_group_photo():
    # cv2.imread в Windows не открывает пути с кириллицей — читаем байты сами.
    return eng.decode_image(GROUP_PHOTO.read_bytes())


# --- Геометрия (без нейросетей) ---
def test_similarity_transform_recovers_known_motion():
    angle, scale, shift = np.deg2rad(25), 1.7, np.array([12.0, -5.0])
    rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    src = geometry.ARCFACE_TEMPLATE.astype(np.float64)
    dst = (scale * src @ rot.T) + shift
    m = geometry.similarity_transform(src, dst)
    assert np.allclose(m[:, :2], scale * rot, atol=1e-4)
    assert np.allclose(m[:, 2], shift, atol=1e-3)


def test_nms_removes_overlaps():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], dtype=np.float32)
    scores = np.array([0.8, 0.9, 0.7], dtype=np.float32)
    assert geometry.nms(boxes, scores, 0.4) == [1, 2]
    assert geometry.nms(np.zeros((0, 4)), np.zeros(0), 0.4) == []


def test_anchor_centers():
    c = geometry.anchor_centers(2, 3, 8, 2)
    assert c.shape == (12, 2)
    assert c[0].tolist() == [0, 0] and c[1].tolist() == [0, 0] and c[2].tolist() == [8, 0]
    assert c[-1].tolist() == [16, 8]


def test_confidence_range():
    a = np.array([1.0, 0.0], dtype=np.float32)
    assert eng.confidence(a, a) == 1.0
    assert eng.confidence(a, -a) == 0.0
    assert eng.confidence(a, np.array([0.0, 1.0], dtype=np.float32)) == 0.5


def test_decode_image_limits():
    with pytest.raises(eng.BadImage):
        eng.decode_image(b"not an image at all")
    with pytest.raises(eng.BadImage):
        eng.decode_image(b"GIF89a" + b"\0" * 100)
    ok, buf = cv2.imencode(".png", np.zeros((20, 30, 3), np.uint8))
    assert eng.decode_image(buf.tobytes()).shape == (20, 30, 3)
    with pytest.raises(eng.BadImage):
        eng.decode_image(b"\xff\xd8\xff" + b"\0" * 50)  # повреждённый JPEG


def test_detection_cache_expiry_and_limit():
    cache = DetectionCache(ttl_seconds=100, max_items=2)
    v = np.zeros(4, np.float32)
    a, b, c = (cache.put(v, (0, 0, 1, 1), 0.9) for _ in range(3))
    assert cache.get(a.id) is None and cache.get(b.id) and cache.get(c.id)
    expired = DetectionCache(ttl_seconds=-1)
    assert expired.get(expired.put(v, (0, 0, 1, 1), 0.9).id) is None


# --- Нейросети ---
@pytest.fixture(scope="module")
def engine():
    return eng.get_engine()


@needs_models
def test_detects_all_faces(engine):
    image = load_group_photo()
    faces = engine.detect_and_extract(image)
    assert len(faces) == 6
    h, w = image.shape[:2]
    for f in faces:
        left, top, right, bottom = f.bbox
        assert 0 <= left < right <= w and 0 <= top < bottom <= h
        assert f.embedding.shape == (512,) and abs(np.linalg.norm(f.embedding) - 1) < 1e-4


@needs_models
def test_different_people_do_not_match(engine):
    from app import config

    faces = engine.detect_and_extract(load_group_photo())
    for i, a in enumerate(faces):
        for b in faces[i + 1:]:
            assert eng.confidence(a.embedding, b.embedding) < config.CONFIDENCE_THRESHOLD


@needs_models
def test_same_person_matches_after_distortion(engine):
    """То же лицо после зеркала, затемнения, размытия, поворота и уменьшения остаётся совпадением."""
    from app import config

    image = load_group_photo()
    original = engine.detect_and_extract(image)
    h, w = image.shape[:2]
    rot = cv2.getRotationMatrix2D((w / 2, h / 2), 8, 0.8)
    distorted = cv2.warpAffine(image, rot, (w, h))
    distorted = cv2.GaussianBlur(cv2.convertScaleAbs(distorted, alpha=0.75, beta=10), (5, 5), 0)
    found = engine.detect_and_extract(distorted)
    assert len(found) == 6
    sims = np.array([[eng.confidence(a.embedding, b.embedding) for b in found] for a in original])
    best = sims.max(axis=1)
    assert (best >= config.CONFIDENCE_THRESHOLD).all(), best
    # Каждому исходному лицу соответствует своё лицо на искажённом фото.
    assert len(set(sims.argmax(axis=1))) == 6


@needs_models
def test_no_faces_on_blank_image(engine):
    assert engine.detect(np.full((480, 640, 3), 127, np.uint8)) == []


# --- API ---
@needs_models
def test_api_detect_and_verify(client, admin):
    with open(GROUP_PHOTO, "rb") as fh:
        r = client.post("/detect/", headers=admin, files={"photo": ("t1.jpg", fh, "image/jpeg")})
    assert r.status_code == 200, r.text
    faces = r.json()["objects"]["face"]
    assert len(faces) == 6 and faces[0]["id"].startswith("detection:")
    areas = [(f["bbox"]["right"] - f["bbox"]["left"]) * (f["bbox"]["bottom"] - f["bbox"]["top"]) for f in faces]
    assert areas == sorted(areas, reverse=True)

    same = client.get("/verify/", headers=admin, params={"object1": faces[0]["id"], "object2": faces[0]["id"]}).json()
    assert same["match"] is True and same["confidence"] > 0.99
    diff = client.get("/verify/", headers=admin, params={"object1": faces[0]["id"], "object2": faces[1]["id"]}).json()
    assert diff["match"] is False and diff["confidence"] < diff["threshold"]


def test_api_errors(client, admin):
    assert client.post("/detect/", files={"photo": ("a.jpg", b"x", "image/jpeg")}).status_code == 401
    assert client.get("/verify/", headers=admin, params={"object1": "detection:nope", "object2": "detection:nope"}).status_code == 404
    assert client.get("/verify/", headers=admin, params={"object1": "face:1", "object2": "face:2"}).status_code == 400
    if model_store.models_present():
        r = client.post("/detect/", headers=admin, files={"photo": ("a.txt", b"hello world", "text/plain")})
        assert r.status_code == 400

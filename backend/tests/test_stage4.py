import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from app import storage
from app.recognition import engine as eng
from app.recognition import geometry, model_store
from app.video import quality
from app.video.processor import VideoError, analyze_video, probe_video
from app.video.tracker import FaceTracker, StreamSettings
from conftest import VIDEO_FPS, login, wait_for_video

GROUP_PHOTO = Path(__file__).parent / "data" / "t1.jpg"

needs_models = pytest.mark.skipif(
    not (model_store.models_present() and GROUP_PHOTO.is_file()),
    reason="нет моделей или тестового фото",
)

FPS = VIDEO_FPS


def fake_face(left, top, size=100):
    return eng.Face((left, top, left + size, top + size), 0.9, geometry.ARCFACE_TEMPLATE.copy())


# --- Трекер (без нейросетей) ---
def test_tracker_follows_two_faces_and_keeps_best_frame():
    tracker = FaceTracker(StreamSettings(max_track_gap=1.0))
    frame = np.zeros((10, 10, 3), np.uint8)
    finished = []
    for i in range(5):  # два лица движутся навстречу друг другу, не пересекаясь
        faces = [(fake_face(100 + 20 * i, 100), 0.3 + 0.1 * i), (fake_face(900 - 20 * i, 400), 0.9 - 0.1 * i)]
        finished += tracker.update(frame, faces, i * 0.2)
    assert finished == []
    finished += tracker.update(frame, [], 1.0)  # ещё не прошло max_track_gap
    assert finished == []
    finished += tracker.update(frame, [], 2.0)
    assert len(finished) == 2
    a, b = sorted(finished, key=lambda t: t.id)
    assert a.frames == b.frames == 5
    assert a.best_quality == pytest.approx(0.7) and a.best_time == pytest.approx(0.8)  # лучший кадр — последний
    assert b.best_quality == pytest.approx(0.9) and b.best_time == 0  # лучший кадр — первый
    assert a.duration == pytest.approx(0.8)


def test_tracker_fast_motion_new_track_and_flush():
    tracker = FaceTracker(StreamSettings())
    frame = np.zeros((10, 10, 3), np.uint8)
    tracker.update(frame, [(fake_face(100, 100), 0.5)], 0.0)
    # Сдвиг на 70% размера рамки: пересечение мало, но центры близко — тот же трек.
    tracker.update(frame, [(fake_face(170, 100), 0.5)], 0.2)
    # Лицо в другом конце кадра — новый трек.
    tracker.update(frame, [(fake_face(170, 100), 0.5), (fake_face(700, 500), 0.5)], 0.4)
    tracks = sorted(tracker.flush(), key=lambda t: t.id)
    assert [t.frames for t in tracks] == [3, 1]
    assert tracker.flush() == []


def test_tracker_max_duration_splits_track():
    tracker = FaceTracker(StreamSettings(max_track_duration=1.0))
    frame = np.zeros((10, 10, 3), np.uint8)
    finished = []
    for i in range(12):
        finished += tracker.update(frame, [(fake_face(100, 100), 0.5)], i * 0.2)
    assert len(finished) == 2  # человек стоит перед камерой 2,2 с — события каждую секунду


# --- Удержание человека (сверка по вектору признаков) ---
def id_face(left, top, ident, size=100):
    """Лицо с «личностью» — её читает FakeEngine при извлечении вектора."""
    face = eng.Face((left, top, left + size, top + size), 0.9, geometry.ARCFACE_TEMPLATE.copy())
    face._ident = ident
    return face


class FakeEngine:
    """Подменяет извлечение вектора: одинаковой личности — один и тот же единичный вектор."""

    def extract(self, frame, faces):
        for face in faces:
            vec = np.zeros(16, np.float32)
            vec[ord(getattr(face, "_ident", "?")) % 16] = 1.0  # одинаковый ident → косинус 1
            face.embedding = vec


def test_hold_keeps_person_through_gap():
    engine = FakeEngine()
    tracker = FaceTracker(StreamSettings(max_track_gap=1.0), hold_seconds=5.0, hold_threshold=0.9)
    frame = np.zeros((10, 10, 3), np.uint8)
    finished = []
    for t in (0.0, 0.2, 0.4):
        finished += tracker.update(frame, [(id_face(100, 100, "A"), 0.5)], t, engine)
    assert finished == []
    # Лицо пропало дольше max_track_gap — человек удерживается, трек не завершён.
    finished += tracker.update(frame, [], 2.0, engine)
    assert finished == [] and tracker.tracks == []
    # Тот же человек вернулся (чуть сместился) — продолжается прежний трек, а не новый.
    finished += tracker.update(frame, [(id_face(115, 100, "A"), 0.6)], 2.2, engine)
    assert finished == [] and len(tracker.tracks) == 1
    assert tracker.tracks[0].frames == 4
    # Окончательно ушёл — одно событие на все 4 кадра.
    finished += tracker.update(frame, [], 8.0, engine)
    assert len(finished) == 1 and finished[0].frames == 4


def test_hold_does_not_merge_different_people():
    engine = FakeEngine()
    tracker = FaceTracker(StreamSettings(max_track_gap=1.0), hold_seconds=5.0, hold_threshold=0.9)
    frame = np.zeros((10, 10, 3), np.uint8)
    for t in (0.0, 0.2):
        tracker.update(frame, [(id_face(100, 100, "A"), 0.5)], t, engine)
    tracker.update(frame, [], 1.5, engine)  # A пропал — удерживается
    # В том же месте появился другой человек — его не принимают за удерживаемого.
    tracker.update(frame, [(id_face(100, 100, "B"), 0.5)], 1.7, engine)
    assert len(tracker.tracks) == 1 and tracker.tracks[0].best_face._ident == "B"
    finished = tracker.update(frame, [], 10.0, engine)
    assert sorted(t.best_face._ident for t in finished) == ["A", "B"]  # два отдельных события


def test_hold_off_without_engine():
    # Без движка (трекинг тел) удержание не работает: трек завершается по max_track_gap.
    tracker = FaceTracker(StreamSettings(max_track_gap=1.0), hold_seconds=5.0, hold_threshold=0.9)
    frame = np.zeros((10, 10, 3), np.uint8)
    tracker.update(frame, [(fake_face(100, 100), 0.5)], 0.0)
    assert len(tracker.update(frame, [], 2.0)) == 1


# --- Качество кадра ---
def test_pose_score_prefers_frontal():
    frontal = geometry.ARCFACE_TEMPLATE.copy()
    turned = frontal.copy()
    turned[2, 0] += 14  # нос смещён в сторону — голова повёрнута
    turned[1, 0] -= 10  # дальний глаз ближе к носу
    assert quality.pose_score(frontal) > 0.9
    assert quality.pose_score(turned) < quality.pose_score(frontal) - 0.3


def test_size_score():
    assert quality.size_score((0, 0, 200, 220)) == 1.0
    assert quality.size_score((0, 0, 55, 60)) == pytest.approx(0.5)


@needs_models
def test_quality_drops_for_blurred_and_small_faces():
    engine = eng.get_engine()
    image = eng.decode_image(GROUP_PHOTO.read_bytes())
    sharp = [quality.face_quality(image, f) for f in engine.detect(image)]
    blurred_image = cv2.GaussianBlur(image, (21, 21), 0)
    blurred = [quality.face_quality(blurred_image, f) for f in engine.detect(blurred_image)]
    small_image = cv2.resize(image, None, fx=0.5, fy=0.5)
    small = [quality.face_quality(small_image, f) for f in engine.detect(small_image)]
    assert len(sharp) == 6 and min(sharp) > 0.6, sharp
    assert len(blurred) == 6 and max(blurred) < min(sharp), (sharp, blurred)
    assert np.mean(small) < np.mean(sharp)


# --- Обработка видео ---
def test_probe_rejects_non_video(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not a video")
    with pytest.raises(VideoError):
        probe_video(bad)


@needs_models
def test_analyze_video_finds_all_tracks(video_file):
    info = probe_video(video_file)
    assert info.fps == pytest.approx(FPS) and info.frame_count == 6 * FPS and (info.width, info.height) == (960, 664)

    progress = []
    results = list(
        analyze_video(video_file, eng.get_engine(), StreamSettings(process_fps=5), lambda p, n: progress.append((p, n)))
    )
    assert len(results) == 12
    assert progress[-1][1] == 30 and progress[-1][0] == pytest.approx(1.0, abs=0.05)  # каждый второй кадр
    first, second = [r for r in results if r.time < 2], [r for r in results if r.time >= 4]
    assert len(first) == len(second) == 6
    for r in results:
        assert r.face.embedding.shape == (512,) and r.frames >= 8 and r.quality >= 0.4
        assert r.duration == pytest.approx(1.8, abs=0.21)
    # Каждый человек из первого прохода узнаётся во втором, и только один раз.
    sims = np.array([[eng.confidence(a.face.embedding, b.face.embedding) for b in second] for a in first])
    assert (sims.max(axis=1) > 0.9).all() and len(set(sims.argmax(axis=1))) == 6


@needs_models
def test_analyze_video_respects_filters_and_stop(video_file):
    engine = eng.get_engine()
    assert list(analyze_video(video_file, engine, StreamSettings(min_quality=1.0))) == []
    assert list(analyze_video(video_file, engine, StreamSettings(min_track_frames=100))) == []
    calls = []
    stopped = list(analyze_video(video_file, engine, StreamSettings(), should_stop=lambda: calls.append(1) or len(calls) >= 3))
    assert stopped == [] and len(calls) == 3


# --- API ---
def default_group(client, admin):
    return client.get("/camera-groups/", headers=admin).json()["results"][0]["id"]


wait_for = wait_for_video


def test_camera_group_crud(client, admin):
    groups = client.get("/camera-groups/", headers=admin).json()["results"]
    assert len(groups) == 1 and groups[0]["predefined"] is True
    r = client.post("/camera-groups/", headers=admin, json={"name": "Проходная", "deduplicate": True, "deduplicate_delay": 30})
    assert r.status_code == 201 and r.json()["deduplicate_delay"] == 30
    gid = r.json()["id"]
    assert client.post("/camera-groups/", headers=admin, json={"name": "Проходная"}).status_code == 409
    assert client.patch(f"/camera-groups/{gid}/", headers=admin, json={"name": "Вход", "face_threshold": 0.8}).json()["face_threshold"] == 0.8
    assert client.delete(f"/camera-groups/{groups[0]['id']}/", headers=admin).status_code == 400
    assert client.delete(f"/camera-groups/{gid}/", headers=admin).status_code == 204


def test_video_upload_validation(client, admin):
    group = default_group(client, admin)

    def upload(name, content, **data):
        return client.post("/videos/", headers=admin, data={"camera_group": group, **data}, files={"source_file": (name, content, "video/mp4")})

    assert upload("notes.txt", b"hello").status_code == 400
    assert upload("fake.mp4", b"not a video at all").status_code == 400
    assert upload("fake.mp4", b"x", camera_group=999).status_code == 404
    assert upload("fake.mp4", b"x", stream_settings='{"process_fps": -1}').status_code == 400
    assert upload("fake.mp4", b"x", stream_settings="{oops").status_code == 400
    assert client.post("/videos/", data={"camera_group": group}, files={"source_file": ("a.mp4", b"x")}).status_code == 401
    # Отклонённые файлы не остаются в хранилище.
    assert not storage.VIDEOS_DIR.exists() or not any(storage.VIDEOS_DIR.iterdir())
    assert client.get("/videos/", headers=admin).json()["count"] == 0


@needs_models
def test_video_processing_end_to_end(client, admin, roles, video_file):
    group = default_group(client, admin)
    with open(video_file, "rb") as fh:
        r = client.post(
            "/videos/", headers=admin,
            data={"camera_group": group, "name": "Проходная, утро", "stream_settings": '{"process_fps": 12}'},
            files={"source_file": ("Запись входа.mp4", fh, "video/mp4")},
        )
    assert r.status_code == 201, r.text
    video = r.json()
    assert video["state"] == "UPLOADED" and video["duration"] == pytest.approx(6.0) and video["width"] == 960
    assert video["stream_settings"] == {} and video["file_size"] > 1000  # значения по умолчанию не дублируются
    vid = video["id"]
    stored = list(storage.VIDEOS_DIR.iterdir())
    assert len(stored) == 1 and "Запись" not in stored[0].name

    assert client.post(f"/videos/{vid}/stop/", headers=admin).status_code == 409
    assert client.post(f"/videos/{vid}/process/", headers=admin).json()["state"] == "QUEUED"
    assert client.post(f"/videos/{vid}/process/", headers=admin).status_code == 409
    assert client.delete(f"/videos/{vid}/", headers=admin).status_code == 409
    done = wait_for(client, admin, vid, {"COMPLETED", "FAILED"})
    assert done["state"] == "COMPLETED", done
    assert done["progress"] == 1.0 and done["events_created"] == 12 and done["frames_processed"] == 60
    assert done["started_date"] and done["finished_date"]

    events = client.get("/events/faces/", headers=admin, params={"video_archive": vid}).json()
    assert events["count"] == 12
    ev = events["results"][0]
    assert ev["camera_group_id"] == group and ev["video_archive_id"] == vid and ev["matched"] is False
    assert 0 <= ev["video_offset"] <= 6 and ev["frame_width"] == 960 and ev["quality"] >= 0.4
    assert "embedding" not in ev
    offsets = [e["video_offset"] for e in events["results"]]
    assert offsets == sorted(offsets, reverse=True)  # новые события первыми
    thumb = client.get(f"/events/faces/{ev['id']}/thumbnail/", headers=admin)
    full = client.get(f"/events/faces/{ev['id']}/fullframe/", headers=admin)
    assert thumb.status_code == full.status_code == 200 and full.content[:3] == b"\xff\xd8\xff"
    assert eng.decode_image(full.content).shape[:2] == (664, 960)
    assert client.get(f"/events/faces/{ev['id']}/thumbnail/").status_code == 401
    assert client.get("/events/faces/", headers=admin, params={"camera_groups": 999}).json()["count"] == 0

    # Пользователь с ролью «Пользователь» видит события, но не управляет видео.
    client.post("/users/", headers=admin, json={"name": "viewer", "password": "viewer-pass-1", "role_id": roles["Пользователь"]["id"]})
    viewer = login(client, "viewer", "viewer-pass-1")
    assert client.get("/events/faces/", headers=viewer).json()["count"] == 12
    assert client.post(f"/videos/{vid}/process/", headers=viewer).status_code == 403
    assert client.delete(f"/videos/{vid}/", headers=viewer).status_code == 403

    # Повторная обработка заменяет события, а не удваивает их.
    client.post(f"/videos/{vid}/process/", headers=admin)
    assert wait_for(client, admin, vid, {"COMPLETED", "FAILED"})["events_created"] == 12
    assert client.get("/events/faces/", headers=admin).json()["count"] == 12

    # Группу с видео удалить нельзя; удаление видео убирает файл и события.
    assert client.delete(f"/camera-groups/{group}/", headers=admin).status_code == 400
    event_id = client.get("/events/faces/", headers=admin).json()["results"][0]["id"]
    assert client.delete(f"/videos/{vid}/", headers=admin).status_code == 204
    assert list(storage.VIDEOS_DIR.iterdir()) == []
    assert client.get("/events/faces/", headers=admin).json()["count"] == 0
    assert storage.event_file(event_id, "thumb") is None and storage.event_file(event_id, "full") is None


@needs_models
def test_video_upload_with_immediate_processing_and_stop(client, admin, video_file):
    group = default_group(client, admin)
    with open(video_file, "rb") as fh:
        r = client.post(
            "/videos/", headers=admin, data={"camera_group": group, "process": "true"},
            files={"source_file": ("scene.mp4", fh, "video/mp4")},
        )
    assert r.status_code == 201 and r.json()["state"] == "QUEUED" and r.json()["name"] == "scene"
    vid = r.json()["id"]
    assert client.post(f"/videos/{vid}/stop/", headers=admin).status_code == 200
    done = wait_for(client, admin, vid, {"CANCELLED", "COMPLETED", "FAILED"})
    assert done["state"] == "CANCELLED" and done["events_created"] < 12
    # После остановки файл можно обработать заново.
    client.post(f"/videos/{vid}/process/", headers=admin)
    assert wait_for(client, admin, vid, {"COMPLETED", "FAILED"})["events_created"] == 12

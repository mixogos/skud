"""Демонстрационный стенд: отдельная база с тестовым администратором, карточками и видео.

Запуск из папки backend:

    .venv/Scripts/python scripts/seed_demo.py

Скрипт создаёт папку data-demo (рабочую базу в data не трогает), заводит администратора
со случайным паролем и записывает учётные данные в data-demo/credentials.txt.
Затем сервер запускается на этих данных:

    SKUD_DATA_DIR=data-demo SKUD_MODELS_DIR=data/models .venv/Scripts/python -m uvicorn app.main:app --port 8010
"""
import os
import secrets
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEMO_DIR = BACKEND / "data-demo"
os.environ["SKUD_DATA_DIR"] = str(DEMO_DIR)
os.environ.setdefault("SKUD_MODELS_DIR", str(BACKEND / "data" / "models"))
sys.path.insert(0, str(BACKEND))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

PHOTO = BACKEND / "tests" / "data" / "t1.jpg"
NAMES = ["Соколова Анна", "Белов Дмитрий", "Орлова Мария"]


def main() -> None:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)

    from app import models, storage
    from app.bootstrap import create_superadmin, init_db
    from app.db import SessionLocal
    from app.recognition import engine as eng
    from app.routers.cards import add_face
    from app.video.worker import process_video

    init_db()
    password = secrets.token_urlsafe(12)
    image = eng.decode_image(PHOTO.read_bytes())
    engine = eng.get_engine()
    faces = sorted(engine.detect_and_extract(image), key=lambda f: f.bbox[0])

    with SessionLocal() as db:
        create_superadmin(db, "demo", password)
        staff = db.query(models.WatchList).first()
        staff.name, staff.color = "Персонал", "#2fd6a3"
        alarm = models.WatchList(name="Нежелательные посетители", color="#ff6464", acknowledge=True, notify=True)
        db.add(alarm)
        group = db.query(models.CameraGroup).first()
        group.name = "Проходная"
        db.flush()
        # Трое из шести — в картотеке: двое сотрудников и один нежелательный посетитель.
        for face, name, lists in zip(faces, NAMES, ([staff], [staff], [alarm])):
            card = models.HumanCard(name=name, watch_lists=lists)
            db.add(card)
            db.flush()
            add_face(db, card, face.embedding, face.bbox, face.detection_score, eng.make_thumbnail(image, face.bbox), eng.encode_jpeg(image))

        # Видео: люди дважды проходят перед камерой.
        photo = cv2.resize(image, (960, 664))
        h, w = photo.shape[:2]
        file_name = "demo.mp4"
        storage.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        path = storage.video_path(file_name)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (w, h))
        for part in (1, 0, -1):
            for i in range(20):
                frame = np.full((h, w, 3), 90, np.uint8) if part == 0 else cv2.warpAffine(
                    photo, np.float32([[1, 0, part * 3 * i], [0, 1, 2 * i]]), (w, h), borderValue=(90, 90, 90))
                writer.write(frame)
        writer.release()
        video = models.VideoArchive(
            name="Проходная, утро", camera_group_id=group.id, file_name=file_name, file_size=path.stat().st_size,
            state="QUEUED", duration=6.0, width=w, height=h,
        )
        db.add(video)
        db.commit()
        video_id = video.id

    process_video(video_id)
    (DEMO_DIR / "credentials.txt").write_text(f"login: demo\npassword: {password}\n", encoding="utf-8")
    print(f"Готово. Учётные данные тестового администратора: {DEMO_DIR / 'credentials.txt'}")


if __name__ == "__main__":
    main()

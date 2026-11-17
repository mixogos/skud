import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from .. import config, models
from ..db import get_db
from ..deps import current_user
from ..recognition import engine as eng
from ..recognition.cache import Detection, detections
from ..settings_store import get_settings

router = APIRouter(tags=["Обнаружение и сравнение лиц"])

DETECTION_PREFIX = "detection:"
FACEOBJECT_PREFIX = "faceobject:"
FACEEVENT_PREFIX = "faceevent:"


class BBox(BaseModel):
    left: int
    top: int
    right: int
    bottom: int


class DetectedFace(BaseModel):
    id: str  # «detection:<id>», действует ограниченное время
    bbox: BBox
    detection_score: float


class DetectOut(BaseModel):
    objects: dict[str, list[DetectedFace]]


class VerifyOut(BaseModel):
    confidence: float
    threshold: float
    match: bool


def engine_dep(_: models.User = Depends(current_user)) -> eng.FaceEngine:
    # Зависит от current_user, чтобы без токена ответом было 401, а не сведения о моделях.
    try:
        return eng.get_engine()
    except eng.ModelsMissing as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


def read_photo(photo: UploadFile) -> np.ndarray:
    """Читает загруженный файл и декодирует его; ошибки формата — ответ 400."""
    try:
        return eng.decode_image(photo.file.read(config.MAX_PHOTO_BYTES + 1))
    except eng.BadImage as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def detect_faces(engine: eng.FaceEngine, image: np.ndarray) -> list[tuple[Detection, DetectedFace]]:
    """Находит лица, кладёт их во временное хранилище. Самое крупное лицо — первым."""
    faces = engine.detect_and_extract(image)
    faces.sort(key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True)
    result = []
    for face in faces:
        det = detections.put(face.embedding, face.bbox, face.detection_score, eng.make_thumbnail(image, face.bbox))
        left, top, right, bottom = face.bbox
        result.append(
            (
                det,
                DetectedFace(
                    id=DETECTION_PREFIX + det.id,
                    bbox=BBox(left=left, top=top, right=right, bottom=bottom),
                    detection_score=round(face.detection_score, 4),
                ),
            )
        )
    return result


def resolve_detection(object_id: str) -> Detection:
    if not object_id.startswith(DETECTION_PREFIX):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Ожидается идентификатор вида {DETECTION_PREFIX}<id>")
    det = detections.get(object_id[len(DETECTION_PREFIX):])
    if det is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Объект {object_id} не найден или устарел — повторите /detect/"
        )
    return det


def resolve_embedding(db: DbSession, user: models.User, object_id: str) -> np.ndarray:
    """Вектор признаков по ссылке «detection:<id>» или «faceobject:<id>»."""
    if object_id.startswith(FACEOBJECT_PREFIX):
        if not user.has_perm("faceobject:view"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав: faceobject:view")
        raw = object_id[len(FACEOBJECT_PREFIX):]
        face = db.get(models.FaceObject, int(raw)) if raw.isdigit() else None
        if face is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Объект {object_id} не найден")
        return np.frombuffer(face.embedding, dtype=np.float32)
    if object_id.startswith(FACEEVENT_PREFIX):
        if not user.has_perm("faceevent:view"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав: faceevent:view")
        raw = object_id[len(FACEEVENT_PREFIX):]
        event = db.get(models.FaceEvent, int(raw)) if raw.isdigit() else None
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Объект {object_id} не найден")
        return np.frombuffer(event.embedding, dtype=np.float32)
    if object_id.startswith(DETECTION_PREFIX):
        return resolve_detection(object_id).embedding
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"Ожидается идентификатор вида {DETECTION_PREFIX}<id>, {FACEOBJECT_PREFIX}<id> или {FACEEVENT_PREFIX}<id>",
    )


@router.post("/detect/", response_model=DetectOut)
def detect(photo: UploadFile, engine: eng.FaceEngine = Depends(engine_dep)):
    """Находит лица на фото. Идентификаторы из ответа можно передать в /verify/,
    в поиск по картотеке и в создание фото лица в карточке."""
    return {"objects": {"face": [out for _, out in detect_faces(engine, read_photo(photo))]}}


@router.get("/verify/", response_model=VerifyOut)
def verify(
    object1: str = Query(description="detection:<id>, faceobject:<id> или faceevent:<id>"),
    object2: str = Query(description="detection:<id>, faceobject:<id> или faceevent:<id>"),
    db: DbSession = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Сравнивает два лица и возвращает степень схожести от 0 до 1."""
    value = eng.confidence(resolve_embedding(db, user, object1), resolve_embedding(db, user, object2))
    threshold = get_settings(db).face_confidence_threshold
    return VerifyOut(confidence=round(value, 4), threshold=threshold, match=value >= threshold)

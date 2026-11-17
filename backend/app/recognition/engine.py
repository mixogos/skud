"""Ядро распознавания: обнаружение лиц и извлечение векторов признаков.

Аналог сервиса findface-extraction-api. Работает на процессоре через ONNX Runtime.
"""
import threading
from dataclasses import dataclass

import cv2
import numpy as np

from .. import config
from . import geometry, model_store


class ModelsMissing(RuntimeError):
    pass


class BadImage(ValueError):
    pass


@dataclass
class Face:
    bbox: tuple[int, int, int, int]  # left, top, right, bottom в пикселях исходного фото
    detection_score: float
    landmarks: np.ndarray  # 5 точек (x, y)
    embedding: np.ndarray | None = None  # нормированный вектор из 512 чисел


def decode_image(data: bytes) -> np.ndarray:
    """Декодирует JPEG/PNG/WEBP в массив BGR с проверкой ограничений."""
    if len(data) > config.MAX_PHOTO_BYTES:
        raise BadImage(f"Файл больше {config.MAX_PHOTO_BYTES // (1024 * 1024)} МБ")
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        raise BadImage("Поддерживаются только изображения JPEG, PNG и WEBP")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise BadImage("Не удалось прочитать изображение")
    if max(image.shape[:2]) > config.MAX_PHOTO_SIDE:
        raise BadImage(f"Изображение больше {config.MAX_PHOTO_SIDE} пикселей по большей стороне")
    return image


def encode_jpeg(image: np.ndarray, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise BadImage("Не удалось сохранить изображение")
    return buf.tobytes()


def make_thumbnail(image: np.ndarray, bbox, margin: float = 0.35, max_side: int = 320) -> bytes:
    """Миниатюра лица: рамка с полями, не больше max_side пикселей, в JPEG."""
    h, w = image.shape[:2]
    left, top, right, bottom = bbox
    mx, my = (right - left) * margin, (bottom - top) * margin
    x1, y1 = max(0, int(left - mx)), max(0, int(top - my))
    x2, y2 = min(w, int(right + mx)), min(h, int(bottom + my))
    crop = image[y1:y2, x1:x2]
    scale = max_side / max(crop.shape[:2])
    if scale < 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return encode_jpeg(crop, 88)


def confidence(a: np.ndarray, b: np.ndarray) -> float:
    """Схожесть двух векторов в диапазоне 0..1: (косинус + 1) / 2."""
    return float(np.clip((float(np.dot(a, b)) + 1.0) / 2.0, 0.0, 1.0))


class FaceEngine:
    DET_SIZE = 640
    STRIDES = (8, 16, 32)
    NUM_ANCHORS = 2
    NMS_IOU = 0.4

    def __init__(self):
        if not model_store.models_present():
            raise ModelsMissing(
                "Модели распознавания не найдены. Выполните: python -m app.cli download-models"
            )
        import onnxruntime as ort

        opts = ort.SessionOptions()
        if config.INFERENCE_THREADS > 0:
            opts.intra_op_num_threads = config.INFERENCE_THREADS
        providers = ["CPUExecutionProvider"]
        self._det = ort.InferenceSession(str(model_store.detector_path()), opts, providers=providers)
        self._rec = ort.InferenceSession(str(model_store.extractor_path()), opts, providers=providers)
        self._det_input = self._det.get_inputs()[0].name
        self._rec_input = self._rec.get_inputs()[0].name

    # --- Обнаружение ---
    def detect(self, image: np.ndarray, threshold: float | None = None) -> list[Face]:
        threshold = config.DETECTION_THRESHOLD if threshold is None else threshold
        h, w = image.shape[:2]
        size = self.DET_SIZE
        scale = size / max(h, w)
        new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
        canvas = np.zeros((size, size, 3), dtype=np.uint8)
        canvas[:new_h, :new_w] = cv2.resize(image, (new_w, new_h))
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128.0, (size, size), (127.5, 127.5, 127.5), swapRB=True)
        outputs = self._det.run(None, {self._det_input: blob})

        n = len(self.STRIDES)
        boxes, scores, points = [], [], []
        for i, stride in enumerate(self.STRIDES):
            sc = outputs[i].reshape(-1)
            bb = outputs[i + n].reshape(-1, 4) * stride
            kp = outputs[i + 2 * n].reshape(-1, 10) * stride
            fm = size // stride
            centers = geometry.anchor_centers(fm, fm, stride, self.NUM_ANCHORS)
            mask = sc >= threshold
            if not mask.any():
                continue
            scores.append(sc[mask])
            boxes.append(geometry.distance_to_boxes(centers[mask], bb[mask]))
            points.append(geometry.distance_to_points(centers[mask], kp[mask]))
        if not scores:
            return []
        scores = np.concatenate(scores)
        boxes = np.concatenate(boxes) / scale
        points = np.concatenate(points) / scale

        faces = []
        for i in geometry.nms(boxes, scores, self.NMS_IOU):
            x1, y1, x2, y2 = boxes[i]
            x1, y1 = max(0, int(round(x1))), max(0, int(round(y1)))
            x2, y2 = min(w, int(round(x2))), min(h, int(round(y2)))
            if min(x2 - x1, y2 - y1) < config.MIN_FACE_SIZE:
                continue
            faces.append(Face((x1, y1, x2, y2), float(scores[i]), points[i].astype(np.float32)))
        return faces

    # --- Вектор признаков ---
    @staticmethod
    def align(image: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
        """Нормализованное изображение лица 112x112."""
        matrix = geometry.similarity_transform(landmarks, geometry.ARCFACE_TEMPLATE)
        return cv2.warpAffine(image, matrix, (112, 112), borderValue=0.0)

    def extract(self, image: np.ndarray, faces: list[Face]) -> None:
        """Заполняет embedding у каждого лица."""
        for face in faces:
            crop = self.align(image, face.landmarks)
            blob = cv2.dnn.blobFromImage(crop, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
            vec = self._rec.run(None, {self._rec_input: blob})[0][0].astype(np.float32)
            norm = float(np.linalg.norm(vec))
            face.embedding = vec / norm if norm > 0 else vec

    def detect_and_extract(self, image: np.ndarray) -> list[Face]:
        faces = self.detect(image)
        self.extract(image, faces)
        return faces


_engine: FaceEngine | None = None
_lock = threading.Lock()


def get_engine() -> FaceEngine:
    """Единственный экземпляр ядра; модели загружаются при первом обращении."""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = FaceEngine()
    return _engine

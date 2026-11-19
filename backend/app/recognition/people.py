"""Детекция людей (силуэтов) и разбор их внешнего вида: пол, одежда, аксессуары, цвет одежды.

Детектор — YOLOv8n (CrowdHuman), один класс «человек». Атрибуты — модель
person-attributes-recognition-crossroad-0234 (ResNet-50, 7 признаков). Цвет одежды
определяется по доминирующему цвету верхней и нижней части силуэта (без отдельной модели).
Оценки приблизительные; на мелких и перекрытых людях ненадёжны.
"""
import threading
from dataclasses import dataclass

import cv2
import numpy as np

from .. import config
from . import model_store

DET_SIZE = 640
DET_CONF = 0.35
DET_NMS = 0.5
# Порядок атрибутов модели crossroad-0234.
ATTR_KEYS = ["male", "bag", "hat", "longsleeves", "longpants", "longhair", "coat"]

# Названия цветов одежды (ключи; подписи — на стороне интерфейса).
_HUE_NAMES = [
    (10, "red"), (22, "orange"), (33, "yellow"), (85, "green"),
    (105, "cyan"), (130, "blue"), (150, "purple"), (170, "pink"), (180, "red"),
]


@dataclass
class PersonBox:
    bbox: tuple[int, int, int, int]  # left, top, right, bottom в пикселях
    score: float


def _session(file_name: str):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    if config.INFERENCE_THREADS > 0:
        opts.intra_op_num_threads = config.INFERENCE_THREADS
    session = ort.InferenceSession(str(model_store.models_dir() / file_name), opts, providers=["CPUExecutionProvider"])
    return session, session.get_inputs()[0].name


def _color_name(bgr: np.ndarray) -> str:
    """Грубое название цвета по среднему BGR: чёрный/серый/белый или цветной по тону."""
    hsv = cv2.cvtColor(np.uint8([[bgr]]), cv2.COLOR_BGR2HSV)[0][0]
    h, s, v = int(hsv[0]), int(hsv[1]), int(hsv[2])
    if v < 55:
        return "black"
    if s < 45:
        return "white" if v > 180 else "gray"
    if v < 90 and h in range(5, 25):
        return "brown"
    for bound, name in _HUE_NAMES:
        if h <= bound:
            return name
    return "red"


def _dominant_color(region: np.ndarray) -> str:
    """Доминирующий цвет области: берём центральную часть (меньше фона) и усредняем."""
    if region.size == 0:
        return "unknown"
    h, w = region.shape[:2]
    band = region[:, w // 4: w - w // 4] if w >= 8 else region
    band = band.reshape(-1, 3)
    return _color_name(band.mean(axis=0))


class PeopleAnalyzer:
    def __init__(self):
        self._det = _session(model_store.PERSON_DETECTOR_FILE)
        self._attr = _session(model_store.PERSON_ATTR_FILE) if model_store.person_attr_present() else None

    # --- Детекция силуэтов ---
    def detect(self, image: np.ndarray) -> list[PersonBox]:
        session, input_name = self._det
        height, width = image.shape[:2]
        scale = DET_SIZE / max(height, width)
        nh, nw = int(round(height * scale)), int(round(width * scale))
        canvas = np.full((DET_SIZE, DET_SIZE, 3), 114, np.uint8)
        canvas[:nh, :nw] = cv2.resize(image, (nw, nh))
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = session.run(None, {input_name: blob})[0][0].T  # [N, 5]: cx, cy, w, h, score

        boxes, scores = [], []
        for cx, cy, bw, bh, sc in out:
            if sc < DET_CONF:
                continue
            x1, y1 = (cx - bw / 2) / scale, (cy - bh / 2) / scale
            x2, y2 = (cx + bw / 2) / scale, (cy + bh / 2) / scale
            boxes.append([x1, y1, x2, y2])
            scores.append(float(sc))
        if not boxes:
            return []
        keep = cv2.dnn.NMSBoxes([[b[0], b[1], b[2] - b[0], b[3] - b[1]] for b in boxes], scores, DET_CONF, DET_NMS)
        result = []
        for i in np.array(keep).reshape(-1):
            x1, y1, x2, y2 = boxes[i]
            result.append(PersonBox(
                (max(0, int(x1)), max(0, int(y1)), min(width, int(x2)), min(height, int(y2))), scores[i]
            ))
        return result

    # --- Атрибуты и цвет одежды ---
    def attributes(self, image: np.ndarray, bbox) -> dict:
        left, top, right, bottom = bbox
        crop = image[top:bottom, left:right]
        if crop.size == 0:
            return {}
        features: dict = {}
        if self._attr is not None:
            session, input_name = self._attr
            inp = cv2.resize(crop, (80, 160)).astype(np.float32)[None]  # NHWC, BGR, значения 0..255
            raw = session.run(None, {input_name: inp})[0].reshape(-1)
            features["gender"] = {
                "name": "male" if raw[0] > 0.5 else "female",
                "confidence": round(float(raw[0] if raw[0] > 0.5 else 1 - raw[0]), 3),
            }
            # Надёжные признаки (F1>0.7) показываем; слабые (сумка, рукава) оставляем с оценкой.
            features["attrs"] = {ATTR_KEYS[i]: round(float(raw[i]), 3) for i in range(1, 7)}
        # Цвет одежды: верхняя треть-половина и нижняя часть силуэта.
        h = crop.shape[0]
        features["upper_color"] = _dominant_color(crop[int(h * 0.18):int(h * 0.5)])
        features["lower_color"] = _dominant_color(crop[int(h * 0.5):int(h * 0.88)])
        return features


_analyzer: PeopleAnalyzer | None = None
_lock = threading.Lock()


def get_people() -> PeopleAnalyzer | None:
    """Единственный экземпляр; None, если нет модели детектора людей."""
    global _analyzer
    if _analyzer is None:
        if not model_store.person_detector_present():
            return None
        with _lock:
            if _analyzer is None:
                _analyzer = PeopleAnalyzer()
    return _analyzer

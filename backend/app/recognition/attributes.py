"""Атрибуты лица: возраст, пол и наличие медицинской маски.

Возраст и пол — модель genderage из набора InsightFace buffalo_l (только некоммерческое
использование). Маска — классификатор MobileNetV2 из проекта Face-Mask-Detection (MIT).
Оценки приблизительные: возраст ошибается на несколько лет, маска определяется хуже
на мелких и повёрнутых лицах.
"""
import threading

import cv2
import numpy as np

from .. import config
from . import model_store


def _session(file_name: str):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    if config.INFERENCE_THREADS > 0:
        opts.intra_op_num_threads = config.INFERENCE_THREADS
    session = ort.InferenceSession(str(model_store.models_dir() / file_name), opts, providers=["CPUExecutionProvider"])
    return session, session.get_inputs()[0].name


def _softmax(logits: np.ndarray) -> np.ndarray:
    e = np.exp(logits - logits.max())
    return e / e.sum()


class AttributeAnalyzer:
    def __init__(self):
        self._genderage = _session(model_store.GENDERAGE_FILE) if model_store.genderage_present() else None
        self._mask = _session(model_store.MASK_FILE) if model_store.mask_present() else None

    def _age_gender(self, image: np.ndarray, bbox) -> dict:
        session, input_name = self._genderage
        left, top, right, bottom = bbox
        # Квадрат 96x96 вокруг центра лица с запасом в полтора размера рамки.
        scale = 96.0 / (max(right - left, bottom - top, 1) * 1.5)
        cx, cy = (left + right) / 2, (top + bottom) / 2
        matrix = np.float32([[scale, 0, 48 - cx * scale], [0, scale, 48 - cy * scale]])
        crop = cv2.warpAffine(image, matrix, (96, 96), borderValue=0.0)
        blob = cv2.dnn.blobFromImage(crop, 1.0, (96, 96), (0, 0, 0), swapRB=True)
        out = session.run(None, {input_name: blob})[0][0]
        probs = _softmax(out[:2].astype(np.float64))
        male = bool(probs[1] >= probs[0])
        return {
            "age": int(np.clip(round(float(out[2]) * 100), 0, 100)),
            "gender": {"name": "male" if male else "female", "confidence": round(float(probs.max()), 4)},
        }

    def _medmask(self, image: np.ndarray, bbox) -> dict:
        session, input_name = self._mask
        height, width = image.shape[:2]
        left, top, right, bottom = bbox
        mx, my = (right - left) * 0.1, (bottom - top) * 0.1
        crop = image[max(0, int(top - my)):min(height, int(bottom + my)), max(0, int(left - mx)):min(width, int(right + mx))]
        rgb = cv2.cvtColor(cv2.resize(crop, (224, 224)), cv2.COLOR_BGR2RGB)
        blob = (rgb.astype(np.float32) / 127.5 - 1.0)[None]  # NHWC, значения -1..1
        probs = session.run(None, {input_name: blob})[0][0]
        masked = bool(probs[0] >= probs[1])  # 0 — в маске, 1 — без маски
        return {"medmask": {"name": "correct" if masked else "none", "confidence": round(float(max(probs[0], probs[1])), 4)}}

    def analyze(self, image: np.ndarray, bbox) -> dict:
        """Словарь атрибутов в формате FindFace: age, gender{name, confidence}, medmask{name, confidence}."""
        features: dict = {}
        if min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < 2:
            return features
        if self._genderage is not None:
            features.update(self._age_gender(image, bbox))
        if self._mask is not None:
            features.update(self._medmask(image, bbox))
        return features


_analyzer: AttributeAnalyzer | None = None
_lock = threading.Lock()


def get_attributes() -> AttributeAnalyzer | None:
    """Единственный экземпляр; None, если не скачана ни одна модель атрибутов."""
    global _analyzer
    if _analyzer is None:
        if not (model_store.genderage_present() or model_store.mask_present()):
            return None
        with _lock:
            if _analyzer is None:
                _analyzer = AttributeAnalyzer()
    return _analyzer

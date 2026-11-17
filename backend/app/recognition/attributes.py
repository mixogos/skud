"""Атрибуты лица: возраст, пол и наличие медицинской маски.

Возраст и пол — модель FairFace (ResNet-34, обучена на сбалансированных данных): точнее
на реальных кадрах, возраст выдаёт группой (например, 30–39). Если её нет — запасная
модель genderage из набора InsightFace (возраст числом). Маска — классификатор MobileNetV2
из проекта Face-Mask-Detection (MIT). Оценки приблизительные; на повёрнутых и мелких лицах
атрибуты не определяются (фильтр — в app/events.py).
"""
import threading

import cv2
import numpy as np

from .. import config
from . import model_store

# FairFace
_FF_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_FF_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
AGE_LABELS = ["0-2", "3-9", "10-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70+"]
AGE_MIDPOINTS = [1, 6, 15, 25, 35, 45, 55, 65, 78]  # число для фильтров по возрасту


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
        self._fairface = None
        if model_store.fairface_present():
            session, input_name = _session(model_store.FAIRFACE_FILE)
            names = [o.name for o in session.get_outputs()]
            self._fairface = (session, input_name, names.index("gender_output"), names.index("age_output"))
        # genderage нужен только как запасной, если FairFace не скачан
        self._genderage = (
            _session(model_store.GENDERAGE_FILE)
            if self._fairface is None and model_store.genderage_present()
            else None
        )
        self._mask = _session(model_store.MASK_FILE) if model_store.mask_present() else None

    # --- Возраст и пол: FairFace (основная модель) ---
    def _age_gender_fairface(self, image: np.ndarray, bbox) -> dict:
        session, input_name, gender_idx, age_idx = self._fairface
        height, width = image.shape[:2]
        left, top, right, bottom = bbox
        bw, bh = right - left, bottom - top
        x1, y1 = max(0, int(left - bw * 0.25)), max(0, int(top - bh * 0.25))
        x2, y2 = min(width, int(right + bw * 0.25)), min(height, int(bottom + bh * 0.25))
        crop = cv2.resize(image[y1:y2, x1:x2], (224, 224))
        rgb = (cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0 - _FF_MEAN) / _FF_STD
        blob = np.transpose(rgb, (2, 0, 1))[None]
        outputs = session.run(None, {input_name: blob})
        gender = _softmax(outputs[gender_idx][0].astype(np.float64))
        age = _softmax(outputs[age_idx][0].astype(np.float64))
        male = int(np.argmax(gender)) == 0  # FairFace: 0 — Male, 1 — Female
        group = int(np.argmax(age))
        return {
            "age": AGE_MIDPOINTS[group],
            "age_group": AGE_LABELS[group],
            "gender": {"name": "male" if male else "female", "confidence": round(float(gender.max()), 4)},
        }

    # --- Возраст и пол: genderage (запасная модель) ---
    def _age_gender_insight(self, image: np.ndarray, bbox) -> dict:
        session, input_name = self._genderage
        left, top, right, bottom = bbox
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

    # --- Медицинская маска ---
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
        """Атрибуты в формате FindFace: age, age_group, gender{name, confidence}, medmask{name, confidence}."""
        features: dict = {}
        if min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < 2:
            return features
        if self._fairface is not None:
            features.update(self._age_gender_fairface(image, bbox))
        elif self._genderage is not None:
            features.update(self._age_gender_insight(image, bbox))
        if self._mask is not None:
            features.update(self._medmask(image, bbox))
        return features


_analyzer: AttributeAnalyzer | None = None
_lock = threading.Lock()


def get_attributes() -> AttributeAnalyzer | None:
    """Единственный экземпляр; None, если не скачана ни одна модель атрибутов."""
    global _analyzer
    if _analyzer is None:
        if not (model_store.fairface_present() or model_store.genderage_present() or model_store.mask_present()):
            return None
        with _lock:
            if _analyzer is None:
                _analyzer = AttributeAnalyzer()
    return _analyzer

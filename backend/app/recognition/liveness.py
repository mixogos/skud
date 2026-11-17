"""Проверка живости лица: отличает живого человека от фотографии или экрана.

Используются две небольшие сети MiniFASNet (Silent-Face-Anti-Spoofing, лицензия Apache-2.0).
Каждая смотрит на лицо с разным запасом вокруг рамки; их оценки усредняются.
Проверка по одному кадру обычной камеры снижает риск подделки, но не исключает его.
"""
import threading

import cv2
import numpy as np

from .. import config
from . import model_store

# (файл модели, во сколько раз область вокруг лица больше рамки лица)
MODELS = ((model_store.LIVENESS_FILES[0], 2.7), (model_store.LIVENESS_FILES[1], 4.0))
REAL_CLASS = 1


def _crop(image: np.ndarray, bbox, scale: float) -> np.ndarray:
    height, width = image.shape[:2]
    left, top, right, bottom = bbox
    w, h = max(1, right - left), max(1, bottom - top)
    scale = min((height - 1) / h, (width - 1) / w, scale)
    cx, cy = left + w / 2, top + h / 2
    x1, y1 = max(0, int(cx - w * scale / 2)), max(0, int(cy - h * scale / 2))
    x2, y2 = min(width - 1, int(cx + w * scale / 2)), min(height - 1, int(cy + h * scale / 2))
    return image[y1:y2 + 1, x1:x2 + 1]


class LivenessChecker:
    def __init__(self):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        if config.INFERENCE_THREADS > 0:
            opts.intra_op_num_threads = config.INFERENCE_THREADS
        self._models = []
        for file_name, scale in MODELS:
            session = ort.InferenceSession(
                str(model_store.models_dir() / file_name), opts, providers=["CPUExecutionProvider"]
            )
            self._models.append((session, session.get_inputs()[0].name, scale))

    def score(self, image: np.ndarray, bbox) -> float:
        """Вероятность того, что лицо настоящее, 0..1."""
        total = 0.0
        for session, input_name, scale in self._models:
            crop = cv2.resize(_crop(image, bbox, scale), (80, 80))
            blob = crop.astype(np.float32).transpose(2, 0, 1)[None]  # BGR, значения 0..255
            logits = session.run(None, {input_name: blob})[0][0]
            probs = np.exp(logits - logits.max())
            total += float(probs[REAL_CLASS] / probs.sum())
        return round(total / len(self._models), 4)


_checker: LivenessChecker | None = None
_lock = threading.Lock()


def get_liveness() -> LivenessChecker | None:
    """Единственный экземпляр; None, если файлы моделей не скачаны."""
    global _checker
    if _checker is None:
        if not model_store.liveness_present():
            return None
        with _lock:
            if _checker is None:
                _checker = LivenessChecker()
    return _checker

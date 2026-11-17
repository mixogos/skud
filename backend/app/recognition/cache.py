"""Временное хранилище результатов обнаружения.

/detect/ возвращает идентификаторы найденных лиц; по ним /verify/ (а позже создание
карточек и поиск) получает вектор признаков без повторной загрузки фото.
"""
import secrets
import threading
import time
from dataclasses import dataclass

import numpy as np

from .. import config


@dataclass
class Detection:
    id: str
    embedding: np.ndarray
    bbox: tuple[int, int, int, int]
    detection_score: float
    expires: float
    thumbnail: bytes = b""


class DetectionCache:
    def __init__(self, ttl_seconds: float, max_items: int = 2000):
        self._ttl = ttl_seconds
        self._max = max_items
        self._items: dict[str, Detection] = {}
        self._lock = threading.Lock()

    def put(self, embedding: np.ndarray, bbox, detection_score: float, thumbnail: bytes = b"") -> Detection:
        now = time.monotonic()
        det = Detection(secrets.token_hex(12), embedding, tuple(bbox), detection_score, now + self._ttl, thumbnail)
        with self._lock:
            for key in [k for k, v in self._items.items() if v.expires < now]:
                del self._items[key]
            while len(self._items) >= self._max:
                del self._items[next(iter(self._items))]  # вытесняем самое старое
            self._items[det.id] = det
        return det

    def get(self, det_id: str) -> Detection | None:
        with self._lock:
            det = self._items.get(det_id)
            if det and det.expires < time.monotonic():
                del self._items[det_id]
                return None
            return det


detections = DetectionCache(config.DETECTION_TTL_MINUTES * 60)

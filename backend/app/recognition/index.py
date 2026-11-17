"""Поисковый индекс по лицам картотеки (аналог базы векторов признаков).

Все векторы держатся в памяти одной матрицей; поиск — одно матричное умножение.
Для десятков тысяч лиц этого достаточно; при росте заменяется на FAISS без смены интерфейса.
"""
import threading

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import models

DIM = 512


class FaceIndex:
    def __init__(self):
        self._lock = threading.Lock()
        self._dirty = True
        self._face_ids = np.zeros(0, dtype=np.int64)
        self._card_ids = np.zeros(0, dtype=np.int64)
        self._matrix = np.zeros((0, DIM), dtype=np.float32)

    def invalidate(self) -> None:
        """Вызывается после любого изменения фото лиц в картотеке."""
        with self._lock:
            self._dirty = True

    def _snapshot(self, db: DbSession):
        with self._lock:
            if self._dirty:
                rows = db.execute(
                    select(models.FaceObject.id, models.FaceObject.card_id, models.FaceObject.embedding)
                ).all()
                self._face_ids = np.array([r[0] for r in rows], dtype=np.int64)
                self._card_ids = np.array([r[1] for r in rows], dtype=np.int64)
                self._matrix = (
                    np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows])
                    if rows
                    else np.zeros((0, DIM), dtype=np.float32)
                )
                self._dirty = False
            return self._matrix, self._card_ids, self._face_ids

    def search_cards(self, db: DbSession, vector: np.ndarray, threshold: float) -> dict[int, tuple[float, int]]:
        """Карточки, у которых хотя бы одно лицо похоже на vector не ниже порога.

        Возвращает {id карточки: (наибольшая схожесть 0..1, id самого похожего фото лица)}.
        """
        matrix, card_ids, face_ids = self._snapshot(db)
        if len(card_ids) == 0:
            return {}
        conf = np.clip((matrix @ vector.astype(np.float32) + 1.0) / 2.0, 0.0, 1.0)
        best: dict[int, tuple[float, int]] = {}
        for i in np.nonzero(conf >= threshold)[0]:
            cid, value = int(card_ids[i]), float(conf[i])
            if cid not in best or value > best[cid][0]:
                best[cid] = (value, int(face_ids[i]))
        return best


face_index = FaceIndex()

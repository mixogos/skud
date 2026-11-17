"""Геометрия без нейросетей: выравнивание лица, подавление дублей рамок, декодирование якорей."""
import numpy as np

# Эталонное положение пяти точек (глаза, нос, уголки рта) на изображении 112x112 для ArcFace.
ARCFACE_TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


def similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Матрица 2x3 преобразования подобия (поворот, масштаб, сдвиг), переводящего src в dst.

    Метод Умеямы: решение по наименьшим квадратам.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_c, dst_c = src - src_mean, dst - dst_mean
    cov = dst_c.T @ src_c / len(src)
    u, s, vt = np.linalg.svd(cov)
    d = np.ones(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        d[1] = -1  # исключаем зеркальное отражение
    rot = u @ np.diag(d) @ vt
    var = (src_c**2).sum() / len(src)
    scale = (s * d).sum() / var if var > 0 else 1.0
    m = np.zeros((2, 3))
    m[:, :2] = scale * rot
    m[:, 2] = dst_mean - scale * rot @ src_mean
    return m.astype(np.float32)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Подавление немаксимумов. Возвращает индексы оставленных рамок по убыванию оценки."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        rest = order[1:]
        w = np.clip(np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]), 0, None)
        h = np.clip(np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]), 0, None)
        inter = w * h
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_threshold]
    return keep


def anchor_centers(height: int, width: int, stride: int, num_anchors: int) -> np.ndarray:
    """Центры якорей SCRFD для карты признаков height x width."""
    ys, xs = np.mgrid[:height, :width]
    centers = (np.stack([xs, ys], axis=-1).astype(np.float32) * stride).reshape(-1, 2)
    return np.repeat(centers, num_anchors, axis=0)


def distance_to_boxes(centers: np.ndarray, dist: np.ndarray) -> np.ndarray:
    return np.stack(
        [
            centers[:, 0] - dist[:, 0],
            centers[:, 1] - dist[:, 1],
            centers[:, 0] + dist[:, 2],
            centers[:, 1] + dist[:, 3],
        ],
        axis=-1,
    )


def distance_to_points(centers: np.ndarray, dist: np.ndarray) -> np.ndarray:
    pts = dist.reshape(len(dist), -1, 2) + centers[:, None, :]
    return pts

"""Оценка качества изображения лица (0..1) для выбора лучшего кадра трека.

Эвристика без отдельной нейросети: учитывает поворот головы (по ключевым точкам),
размер лица и резкость. Чем выше оценка, тем надёжнее распознавание по этому кадру.
"""
import cv2
import numpy as np

from ..recognition.engine import Face, FaceEngine


def pose_score(landmarks: np.ndarray) -> float:
    """1 — лицо анфас, 0 — сильный поворот или наклон."""
    left_eye, right_eye, nose, left_mouth, right_mouth = landmarks
    eye_mid = (left_eye + right_eye) / 2
    mouth_mid = (left_mouth + right_mouth) / 2
    eye_dist = float(np.linalg.norm(right_eye - left_eye))
    face_height = float(np.linalg.norm(mouth_mid - eye_mid))
    if eye_dist < 1e-3 or face_height < 1e-3:
        return 0.0
    # Поворот влево-вправо: нос смещается от середины между глазами.
    yaw = abs(float(nose[0] - eye_mid[0])) / eye_dist
    yaw_score = float(np.clip(1.0 - yaw / 0.45, 0.0, 1.0))
    # Наклон вверх-вниз: меняется положение носа между линией глаз и линией рта.
    ratio = float(np.dot(nose - eye_mid, mouth_mid - eye_mid)) / face_height**2
    pitch_score = float(np.clip(1.0 - abs(ratio - 0.55) / 0.4, 0.0, 1.0))
    # В профиль расстояние между глазами сжимается относительно высоты лица.
    profile_score = float(np.clip((eye_dist / face_height - 0.5) / 0.4, 0.0, 1.0))
    return 0.5 * yaw_score + 0.25 * pitch_score + 0.25 * profile_score


def sharpness_score(aligned: np.ndarray) -> float:
    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return float(np.clip(np.sqrt(variance) / 14.0, 0.0, 1.0))


def size_score(bbox) -> float:
    left, top, right, bottom = bbox
    return float(np.clip(min(right - left, bottom - top) / 110.0, 0.0, 1.0))


def face_quality(image: np.ndarray, face: Face) -> float:
    aligned = FaceEngine.align(image, face.landmarks)
    value = 0.45 * pose_score(face.landmarks) + 0.35 * sharpness_score(aligned) + 0.20 * size_score(face.bbox)
    return round(float(value), 4)

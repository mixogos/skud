"""Файлы нейросетевых моделей: где лежат и как скачать.

Используется набор InsightFace buffalo_l (только для некоммерческого использования):
  det_10g.onnx   — детектор лиц SCRFD с пятью ключевыми точками
  w600k_r50.onnx — экстрактор вектора признаков ArcFace (512 чисел)

Проверка живости — MiniFASNet (Silent-Face-Anti-Spoofing, Apache-2.0), два файла по 1,7 МБ.

Атрибуты лица:
  genderage.onnx     — возраст и пол, из того же набора buffalo_l (1,3 МБ)
  mask_detector.onnx — медицинская маска, MobileNetV2 из проекта Face-Mask-Detection, MIT (9,6 МБ)
"""
import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .. import config

PACK_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
DETECTOR_FILE = "det_10g.onnx"
EXTRACTOR_FILE = "w600k_r50.onnx"

LIVENESS_URL = "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/"
LIVENESS_FILES = ("MiniFASNetV2.onnx", "MiniFASNetV1SE.onnx")
LIVENESS_SHA256 = {
    "MiniFASNetV2.onnx": "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907",
    "MiniFASNetV1SE.onnx": "ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676",
}


GENDERAGE_FILE = "genderage.onnx"
MASK_FILE = "mask_detector.onnx"
# FairFace (ResNet-34, обучена на сбалансированных данных) — точнее по возрасту и полу, чем genderage.
FAIRFACE_FILE = "fairface.onnx"
ATTRIBUTE_SOURCES = {
    FAIRFACE_FILE: (
        "https://github.com/yakhyo/fairface-onnx/releases/download/weights/fairface.onnx",
        "9c8c47d437cd310538d233f2465f9ed0524cb7fb51882a37f74e8bc22437fdbf",
    ),
    GENDERAGE_FILE: (
        "https://huggingface.co/public-data/insightface/resolve/main/models/buffalo_l/genderage.onnx",
        "4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb",
    ),
    MASK_FILE: (
        "https://huggingface.co/Dao-Dung/face-mask-detector-onnx/resolve/main/mask_detector.onnx",
        "fcc9e8d5a19b8000e11fbae4e2cb40315c477c32c5dbf533d3a00f84fd258c00",
    ),
}


def models_dir() -> Path:
    return config.MODELS_DIR


def detector_path() -> Path:
    return models_dir() / DETECTOR_FILE


def extractor_path() -> Path:
    return models_dir() / EXTRACTOR_FILE


def models_present() -> bool:
    return detector_path().is_file() and extractor_path().is_file()


def liveness_present() -> bool:
    return all((models_dir() / name).is_file() for name in LIVENESS_FILES)


def genderage_present() -> bool:
    return (models_dir() / GENDERAGE_FILE).is_file()


def mask_present() -> bool:
    return (models_dir() / MASK_FILE).is_file()


def fairface_present() -> bool:
    return (models_dir() / FAIRFACE_FILE).is_file()


def _fetch_verified(name: str, url: str, sha256: str, progress) -> None:
    target = models_dir() / name
    if target.is_file():
        return
    progress(f"Скачиваю {url} ...")
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read(64 << 20)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(f"Контрольная сумма файла {name} не совпала — файл не сохранён")
    target.write_bytes(data)


def download_liveness(progress=print) -> None:
    """Скачивает модели проверки живости и сверяет контрольные суммы."""
    models_dir().mkdir(parents=True, exist_ok=True)
    for name in LIVENESS_FILES:
        _fetch_verified(name, LIVENESS_URL + name, LIVENESS_SHA256[name], progress)
    progress("Модели проверки живости на месте.")


def download_attributes(progress=print) -> None:
    """Скачивает модели атрибутов лица (возраст, пол, маска) и сверяет контрольные суммы."""
    models_dir().mkdir(parents=True, exist_ok=True)
    for name, (url, sha256) in ATTRIBUTE_SOURCES.items():
        _fetch_verified(name, url, sha256, progress)
    progress("Модели атрибутов лица на месте.")


def download(progress=print) -> None:
    """Скачивает архив моделей и распаковывает из него два нужных файла."""
    if models_present():
        progress("Модели уже на месте.")
        return
    models_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=models_dir()) as tmp:
        archive = Path(tmp) / "pack.zip"
        progress(f"Скачиваю {PACK_URL} (около 280 МБ)...")
        with urllib.request.urlopen(PACK_URL, timeout=60) as resp, open(archive, "wb") as out:
            shutil.copyfileobj(resp, out, length=1 << 20)
        with zipfile.ZipFile(archive) as zf:
            wanted = {DETECTOR_FILE, EXTRACTOR_FILE}
            for member in zf.infolist():
                name = Path(member.filename).name
                if name in wanted:
                    # Пишем под фиксированным именем — пути из архива не используются.
                    with zf.open(member) as src, open(models_dir() / name, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=1 << 20)
                    wanted.discard(name)
            if wanted:
                raise RuntimeError(f"В архиве нет файлов: {', '.join(sorted(wanted))}")
    progress(f"Модели сохранены в {models_dir()}")

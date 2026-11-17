# PyInstaller: сборка СКУД в автономное оконное приложение (папка dist/SKUD, запуск SKUD.exe).
# Сборка:  python -m PyInstaller --noconfirm --clean packaging/skud.spec   (из корня проекта)
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = os.path.dirname(os.path.abspath(SPECPATH))  # корень проекта (packaging/ лежит в нём)
backend = os.path.join(root, "backend")

# Ресурсы рядом с приложением: модели нейросетей и собранный веб-интерфейс.
datas = [
    (os.path.join(backend, "data", "models"), "models"),
    (os.path.join(root, "frontend", "dist"), "frontend"),
]
binaries = []
hiddenimports = collect_submodules("uvicorn") + ["clr"]

# Пакеты, которые подгружаются неявно (внутри функций) — забираем целиком с их файлами и библиотеками.
for package in ("onnxruntime", "cv2", "webview", "app", "clr_loader"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

icon = os.path.join(root, "packaging", "skud.ico")
icon = icon if os.path.isfile(icon) else None

a = Analysis(
    [os.path.join(backend, "desktop.py")],
    pathex=[backend],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="SKUD",
    console=False,  # без чёрного окна консоли — обычное приложение
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SKUD")

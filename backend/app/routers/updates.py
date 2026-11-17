"""Проверка и установка обновлений приложения (GitHub Releases)."""
from fastapi import APIRouter, Depends

from .. import models, updater
from ..deps import current_user, require

router = APIRouter(tags=["Обновление"])


@router.get("/update/status/")
def update_status(force: bool = False, _: models.User = Depends(current_user)):
    """Текущая версия и есть ли в GitHub версия новее."""
    return updater.status(force=force)


@router.post("/update/apply/")
def update_apply(_: models.User = Depends(require("change_runtimesetting"))):
    """Скачивает и запускает установщик новой версии. Программа закроется и обновится."""
    return updater.apply()

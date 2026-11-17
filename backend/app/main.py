import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .bootstrap import init_db
from .notifications import broadcaster
from .routers import (
    analytics,
    auditlogs,
    auth,
    cards,
    detectors,
    face_events,
    recognition,
    reports,
    roles,
    settings,
    updates,
    users,
    video,
    watch_lists,
)
from .version import __version__
from .routers import webhooks as webhooks_router
from .video.streams import stream_manager
from .video.worker import video_worker
from .webhooks import sender as webhook_sender


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    broadcaster.bind(asyncio.get_running_loop())
    webhook_sender.start()
    if config.VIDEO_WORKER_ENABLED:
        video_worker.start()
    if config.STREAMS_ENABLED:
        stream_manager.start()
    yield
    stream_manager.stop()
    video_worker.stop()
    webhook_sender.stop()
    broadcaster.unbind()


app = FastAPI(
    title="СКУД — система распознавания лиц",
    version=__version__,
    description="HTTP API. Авторизация: заголовок «Authorization: Token <токен>», токен выдаёт POST /auth/login/.",
    lifespan=lifespan,
    docs_url="/api-docs",
    redoc_url=None,
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(auditlogs.router)
app.include_router(settings.router)
app.include_router(updates.router)
app.include_router(recognition.router)
app.include_router(watch_lists.router)
app.include_router(cards.router)
app.include_router(video.router)
app.include_router(detectors.router)
app.include_router(analytics.router)
app.include_router(webhooks_router.router)
app.include_router(reports.router)
app.include_router(face_events.router)


@app.get("/healthz", tags=["Служебное"])
def healthz():
    return {"status": "ok", "version": app.version}


# Веб-интерфейс: собранные файлы из frontend/dist (npm run build). Маршруты интерфейса
# живут после «#» в адресе, поэтому с путями API не пересекаются.
if (config.FRONTEND_DIR / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIR / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(config.FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})

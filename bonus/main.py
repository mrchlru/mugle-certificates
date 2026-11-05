"""Точка входа: один процесс для карточки, кассы и HR."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.types import Scope

from bonus import __version__
from bonus.accrual import accrual_worker
from bonus.bootstrap import bootstrap
from bonus.config import get_settings
from bonus.db import configure_database, open_session
from bonus.http import PACKAGE_DIR, Redirect
from bonus.routes_auth import router as auth_router
from bonus.routes_card import router as card_router
from bonus.routes_desk import router as desk_router
from bonus.routes_hr import router as hr_router
from bonus.routes_iiko import router as iiko_router
from bonus.routes_kassy import router as kassy_router
from bonus.routes_spasibo import router as spasibo_router
from bonus.sheets_queue import sheets_queue_worker


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    settings = get_settings()
    settings.data_path().mkdir(parents=True, exist_ok=True)
    (settings.data_path() / "uploads").mkdir(parents=True, exist_ok=True)
    configure_database(settings.normalized_database_url())
    bootstrap(settings)
    workers = [
        asyncio.create_task(accrual_worker(), name="bonus-accrual"),
        asyncio.create_task(sheets_queue_worker(), name="bonus-sheets-queue"),
    ]
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    """Собирает сайт, статику и четыре раздела."""
    settings = get_settings()
    uploads = settings.data_path() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Teams Bonus — сертификаты", version=__version__, lifespan=_lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="mugle_bonus",
        max_age=60 * 60 * 24 * 14,
        same_site="lax",
        https_only=settings.https_only,
    )
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.mount("/media", CachedStaticFiles(directory=str(uploads)), name="media")
    app.include_router(auth_router)
    app.include_router(card_router)
    app.include_router(desk_router)
    app.include_router(hr_router)
    app.include_router(kassy_router)
    app.include_router(iiko_router)
    app.include_router(spasibo_router)
    app.add_exception_handler(Redirect, _redirect)
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/live", live, methods=["GET"])
    return app


def health() -> JSONResponse:
    """Проверка для Timeweb: процесс жив и база отвечает."""
    try:
        db = open_session()
    except RuntimeError:
        return JSONResponse({"status": "starting"}, status_code=503)
    try:
        db.execute(text("SELECT 1"))
    finally:
        db.close()
    return JSONResponse({"status": "ok"})


def live() -> dict[str, str]:
    """Лёгкая проверка без базы."""
    return {"status": "ok"}


def _redirect(_request: Request, exc: Exception) -> RedirectResponse:
    page = exc if isinstance(exc, Redirect) else Redirect("/")
    return RedirectResponse(page.url, status_code=page.status_code)


class CachedStaticFiles(StaticFiles):
    """Отдаёт медиа с долгим кэшем в браузере."""

    def file_response(
        self,
        full_path: Path | str,
        stat_result: object,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """Добавляет Cache-Control к ответу со статикой."""
        response = super().file_response(full_path, stat_result, scope, status_code=status_code)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app = create_app()

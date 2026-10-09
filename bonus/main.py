"""Точка входа: один процесс для карточки, кассы и HR."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from bonus import __version__
from bonus.bootstrap import bootstrap
from bonus.config import get_settings
from bonus.db import configure_database, open_session
from bonus.http import PACKAGE_DIR, Redirect
from bonus.routes_auth import router as auth_router
from bonus.routes_card import router as card_router
from bonus.routes_desk import router as desk_router
from bonus.routes_hr import router as hr_router


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    settings = get_settings()
    configure_database(settings.normalized_database_url())
    bootstrap(settings)
    yield


def create_app() -> FastAPI:
    """Собирает сайт, статику и четыре раздела."""
    settings = get_settings()
    app = FastAPI(title="Mugle Rest — сертификаты", version=__version__, lifespan=_lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="mugle_bonus",
        max_age=60 * 60 * 24 * 14,
        same_site="lax",
        https_only=settings.https_only,
    )
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.include_router(auth_router)
    app.include_router(card_router)
    app.include_router(desk_router)
    app.include_router(hr_router)
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


app = create_app()

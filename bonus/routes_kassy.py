"""Страница «Кассы» в админке Teams Bonus."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from bonus.db import get_db
from bonus.http import guard_csrf, render, require
from bonus.iiko_monitor import (
    add_expected_restaurant,
    build_dashboard,
    forget_terminal,
    remove_expected_restaurant,
)
from bonus.models import Role, User

router = APIRouter()


@router.get("/hr/kassy")
def kassy_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.admin, Role.hrd)),
):
    """Мониторинг касс iiko."""
    data = build_dashboard(db)
    return render(
        request,
        "hr_kassy.html",
        section="kassy",
        dashboard=data,
    )


@router.get("/hr/kassy/data")
def kassy_data(
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.admin, Role.hrd)),
):
    """JSON для автообновления таблицы касс."""
    return JSONResponse(build_dashboard(db))


@router.post("/hr/kassy/expected")
def kassy_add_expected(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.admin, Role.hrd)),
    name: str = Form(""),
    csrf_token: str = Form(""),
):
    """Добавляет ресторан в список ожидаемых."""
    guard_csrf(request, csrf_token)
    add_expected_restaurant(db, name)
    return RedirectResponse("/hr/kassy", status_code=303)


@router.post("/hr/kassy/expected/{restaurant_id}/delete")
def kassy_remove_expected(
    restaurant_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.admin, Role.hrd)),
    csrf_token: str = Form(""),
):
    """Удаляет ресторан из ожидаемых."""
    guard_csrf(request, csrf_token)
    remove_expected_restaurant(db, restaurant_id)
    return RedirectResponse("/hr/kassy", status_code=303)


@router.post("/hr/kassy/terminals/{terminal_id}/forget")
def kassy_forget_terminal(
    terminal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.admin, Role.hrd)),
    csrf_token: str = Form(""),
):
    """Удаляет запись кассы из мониторинга."""
    guard_csrf(request, csrf_token)
    forget_terminal(db, terminal_id)
    return RedirectResponse("/hr/kassy", status_code=303)

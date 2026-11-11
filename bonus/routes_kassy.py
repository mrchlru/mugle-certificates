"""Страница «Кассы» в админке Teams Bonus."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from bonus.db import get_db
from bonus.http import guard_csrf, render, require
from bonus.iiko_monitor import build_dashboard, forget_terminal, set_terminal_debit_blocked
from bonus.models import Role, User

router = APIRouter()


@router.get("/hr/kassy")
def kassy_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.admin)),
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
    user: User = Depends(require(Role.admin)),
):
    """JSON для автообновления таблицы касс."""
    return JSONResponse(build_dashboard(db))


@router.post("/hr/kassy/terminals/{terminal_id}/debit-block")
def kassy_toggle_debit_block(
    terminal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.admin)),
    csrf_token: str = Form(""),
    blocked: str = Form("1"),
):
    """Блокирует или разрешает списание бонусов с кассы."""
    guard_csrf(request, csrf_token)
    set_terminal_debit_blocked(db, terminal_id, (blocked or "").strip() not in {"", "0", "false"})
    return RedirectResponse("/hr/kassy", status_code=303)


@router.post("/hr/kassy/terminals/{terminal_id}/forget")
def kassy_forget_terminal(
    terminal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.admin)),
    csrf_token: str = Form(""),
):
    """Удаляет запись кассы из мониторинга."""
    guard_csrf(request, csrf_token)
    forget_terminal(db, terminal_id)
    return RedirectResponse("/hr/kassy", status_code=303)

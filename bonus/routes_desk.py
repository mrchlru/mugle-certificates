"""Касса управляющего: найти сертификат и списать бонусы."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from bonus.db import get_db
from bonus.http import form_money, guard_csrf, public_base, render, require
from bonus.models import Role, User
from bonus.present import load_card, recent_debits
from bonus.security import flash, safe_next
from bonus.services import BonusError, debit_certificate, find_certificate, undo_debit

router = APIRouter()


@router.get("/desk")
def desk(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.manager, Role.hr)),
    code: str = "",
):
    """Поле и камера. Код из QR открывает экран списания."""
    if code.strip():
        certificate = find_certificate(db, code)
        if certificate is None:
            flash(request, "Сертификат не найден. Проверьте код.", "bad")
            return RedirectResponse("/desk", status_code=303)
        return RedirectResponse(f"/m/{certificate.token}", status_code=303)
    return render(request, "desk.html", recent=recent_debits(db, user))


@router.get("/m/{token}")
def debit_form(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.manager, Role.hr)),
    done: int = 0,
):
    """Экран списания после сканирования."""
    certificate = find_certificate(db, token)
    if certificate is None:
        flash(request, "Сертификат не найден", "bad")
        return RedirectResponse("/desk", status_code=303)
    card = load_card(db, certificate, public_base(request), user)
    return render(request, "debit.html", card=card, token=certificate.token, done_id=done)


@router.post("/m/{token}")
def debit_submit(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.manager, Role.hr)),
    amount: str = Form(""),
    comment: str = Form(""),
    csrf_token: str = Form(""),
):
    """Списывает введённую сумму."""
    certificate = find_certificate(db, token)
    if certificate is None:
        flash(request, "Сертификат не найден", "bad")
        return RedirectResponse("/desk", status_code=303)
    try:
        guard_csrf(request, csrf_token)
        tx = debit_certificate(db, certificate, user, form_money(amount), comment)
    except BonusError as exc:
        card = load_card(db, certificate, public_base(request), user)
        return render(
            request,
            "debit.html",
            status_code=400,
            card=card,
            token=certificate.token,
            done_id=0,
            error=exc.message,
            amount=amount,
            comment=comment,
        )
    return RedirectResponse(f"/m/{certificate.token}?done={tx.id}", status_code=303)


@router.post("/tx/{tx_id}/undo")
def undo(
    tx_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.manager, Role.hr)),
    csrf_token: str = Form(""),
    next: str = Form("/desk"),
):
    """Возвращает бонусы по ошибочному списанию."""
    target = safe_next(next, "/desk")
    try:
        guard_csrf(request, csrf_token)
        undo_debit(db, tx_id, user, "Отмена списания")
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse(target, status_code=303)
    flash(request, "Списание отменено, бонусы вернулись на сертификат")
    return RedirectResponse(target, status_code=303)

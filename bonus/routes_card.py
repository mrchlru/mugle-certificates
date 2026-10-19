"""Карточка сотрудника с QR-кодом."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from bonus.db import get_db
from bonus.google_wallet import save_url, wallet_enabled
from bonus.http import public_base, render, require
from bonus.models import Role, User
from bonus.present import load_card, load_cards
from bonus.services import find_certificate

router = APIRouter()


@router.get("/card")
def card(request: Request, db: Session = Depends(get_db), user: User = Depends(require(Role.employee))):
    """Показывает сертификаты того, кто вошёл."""
    cards = load_cards(db, user.id, public_base(request), user)
    return render(request, "card.html", cards=cards)


@router.get("/c/{token}")
def public_certificate(token: str, request: Request, db: Session = Depends(get_db)):
    """Ссылка, которую HR отправляет сотруднику. Пароль не нужен."""
    certificate = find_certificate(db, token)
    if certificate is None:
        return render(request, "missing.html", status_code=404)
    base = public_base(request)
    card = load_card(db, certificate, base, None)
    return render(
        request,
        "public_card.html",
        card=card,
        google_wallet_enabled=wallet_enabled(),
        google_wallet_href=f"/c/{certificate.token}/google-wallet",
    )


@router.get("/c/{token}/google-wallet")
def google_wallet_save(token: str, request: Request, db: Session = Depends(get_db)):
    """Редирект на Add to Google Wallet (подписанный JWT)."""
    certificate = find_certificate(db, token)
    if certificate is None:
        return render(request, "missing.html", status_code=404)
    if not wallet_enabled():
        return render(request, "missing.html", status_code=404)
    base = public_base(request)
    url = save_url(certificate, certificate.holder, base_url=base)
    if not url:
        return render(
            request,
            "public_card.html",
            card=load_card(db, certificate, base, None),
            google_wallet_enabled=False,
            google_wallet_href="",
            status_code=503,
        )
    return RedirectResponse(url, status_code=302)

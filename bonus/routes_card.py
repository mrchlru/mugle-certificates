"""Карточка сотрудника с QR-кодом."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from bonus.db import get_db
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
    card = load_card(db, certificate, public_base(request), None)
    return render(request, "public_card.html", card=card)

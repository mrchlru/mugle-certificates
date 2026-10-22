"""Карточка сотрудника с QR-кодом."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from bonus.apple_wallet import (
    apple_wallet_enabled,
    build_pkpass,
    pkpass_download_url,
    walletpasses_url,
)
from bonus.app_settings import certificate_shows_wallet_buttons
from bonus.db import get_db
from bonus.google_wallet import save_url, wallet_enabled
from bonus.http import public_base, render, require
from bonus.models import Certificate, Role, User
from bonus.partners import partner_cities
from bonus.present import load_card, load_cards
from bonus.services import find_certificate
from bonus.weekdays import WEEKDAY_CHOICES

router = APIRouter()


def _card_page_extras() -> dict:
    """Общий контекст: дни недели и каталог партнёров."""
    return {
        "weekday_choices": WEEKDAY_CHOICES,
        "partner_cities": partner_cities(),
    }


def _wallet_ui(db: Session, certificate: Certificate, base: str) -> dict:
    """Флаги и ссылки кнопок кошелька с учётом глобальной настройки и тестовой карты."""
    if not certificate_shows_wallet_buttons(db, certificate):
        return {
            "google_wallet_enabled": False,
            "google_wallet_href": "",
            "apple_wallet_enabled": False,
            "apple_wallet_href": "",
            "walletpasses_enabled": False,
            "walletpasses_href": "",
        }
    apple_on = apple_wallet_enabled()
    pkpass_url = pkpass_download_url(certificate, base) if apple_on else ""
    return {
        "google_wallet_enabled": wallet_enabled(),
        "google_wallet_href": f"/c/{certificate.token}/google-wallet",
        "apple_wallet_enabled": apple_on,
        "apple_wallet_href": f"/c/{certificate.token}/apple-wallet.pkpass" if apple_on else "",
        "walletpasses_enabled": apple_on and bool(pkpass_url),
        "walletpasses_href": walletpasses_url(pkpass_url) if pkpass_url else "",
    }


@router.get("/card")
def card(request: Request, db: Session = Depends(get_db), user: User = Depends(require(Role.employee))):
    """Показывает сертификаты того, кто вошёл."""
    cards = load_cards(db, user.id, public_base(request), user)
    return render(request, "card.html", cards=cards, **_card_page_extras())


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
        card_token=certificate.token,
        **_wallet_ui(db, certificate, base),
        **_card_page_extras(),
    )


@router.post("/c/{token}/gift-opened")
def mark_gift_opened(token: str, db: Session = Depends(get_db)):
    """Снимает флаг распаковки на карте (один раз) и пишет время открытия."""
    certificate = find_certificate(db, token)
    if certificate is None:
        return JSONResponse({"ok": False}, status_code=404)
    changed = False
    if getattr(certificate, "unwrap_pending", False):
        certificate.unwrap_pending = False
        changed = True
    if certificate.gift_opened_at is None:
        certificate.gift_opened_at = datetime.now(timezone.utc)
        changed = True
    if changed:
        db.commit()
    return JSONResponse({"ok": True, "unwrap_pending": False})


@router.get("/c/{token}/google-wallet")
def google_wallet_save(token: str, request: Request, db: Session = Depends(get_db)):
    """Редирект на Add to Google Wallet (подписанный JWT)."""
    certificate = find_certificate(db, token)
    if certificate is None:
        return render(request, "missing.html", status_code=404)
    if not certificate_shows_wallet_buttons(db, certificate) or not wallet_enabled():
        return render(request, "missing.html", status_code=404)
    base = public_base(request)
    url = save_url(certificate, certificate.holder, base_url=base)
    if not url:
        return render(
            request,
            "public_card.html",
            card=load_card(db, certificate, base, None),
            card_token=certificate.token,
            status_code=503,
            **_wallet_ui(db, certificate, base),
            **_card_page_extras(),
        )
    return RedirectResponse(url, status_code=302)


@router.get("/c/{token}/apple-wallet.pkpass")
def apple_wallet_pkpass(token: str, request: Request, db: Session = Depends(get_db)):
    """Скачивание .pkpass для Apple Wallet и WalletPasses (Android)."""
    certificate = find_certificate(db, token)
    if certificate is None:
        return render(request, "missing.html", status_code=404)
    if not certificate_shows_wallet_buttons(db, certificate) or not apple_wallet_enabled():
        return render(request, "missing.html", status_code=404)
    base = public_base(request)
    payload = build_pkpass(certificate, certificate.holder, base_url=base)
    if not payload:
        return render(
            request,
            "public_card.html",
            card=load_card(db, certificate, base, None),
            card_token=certificate.token,
            status_code=503,
            **_wallet_ui(db, certificate, base),
            **_card_page_extras(),
        )
    filename = f"{certificate.public_number or certificate.id}.pkpass"
    return Response(
        content=payload,
        media_type="application/vnd.apple.pkpass",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

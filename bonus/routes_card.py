"""Карточка сотрудника с QR-кодом."""

from dataclasses import replace
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
from bonus.http import public_base, render, require, templates
from bonus.models import Certificate, CertificateStatus, Role, User
from bonus.partners import partner_cities
from bonus.present import HISTORY_PAGE_SIZE, TxView, load_card, load_cards, load_history_page
from bonus.services import find_certificate, repair_iiko_part_balance_drift
from bonus.values import today_msk
from bonus.weekdays import WEEKDAY_CHOICES

router = APIRouter()


def _card_page_extras() -> dict:
    """Общий контекст: дни недели и каталог партнёров."""
    return {
        "weekday_choices": WEEKDAY_CHOICES,
        "partner_cities": partner_cities(),
    }


def _wallet_ui(db: Session, certificate: Certificate, base: str) -> dict:
    """Флаги и ссылки Apple Wallet / Wallet Passes (Google Wallet отключён)."""
    if not certificate_shows_wallet_buttons(db, certificate):
        return {
            "apple_wallet_enabled": False,
            "apple_wallet_href": "",
            "walletpasses_enabled": False,
            "walletpasses_href": "",
        }
    apple_on = apple_wallet_enabled()
    pkpass_url = pkpass_download_url(certificate, base) if apple_on else ""
    return {
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


@router.get("/c/{token}/history")
def public_certificate_history(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    before: str = "",
    limit: int = HISTORY_PAGE_SIZE,
):
    """HTML-порция истории для ленивой подгрузки на карточке."""
    certificate = find_certificate(db, token)
    if certificate is None or _unavailable_reason(certificate) is not None:
        return Response(status_code=404)
    before_id = _parse_before(before)
    if before_id is None:
        return Response(status_code=400)
    page_limit = _clamp_history_limit(limit)
    # Публичная карта — только просмотр: отмена списаний здесь не показывается.
    page = load_history_page(
        db,
        certificate,
        None,
        limit=page_limit,
        before_id=before_id,
    )
    return _history_fragment_response(
        request,
        items=page.items,
        csrf="",
        has_more=page.has_more,
        next_before=page.next_before,
    )


@router.get("/c/{token}")
def public_certificate(token: str, request: Request, db: Session = Depends(get_db)):
    """Ссылка, которую HR отправляет сотруднику. Пароль не нужен."""
    certificate = find_certificate(db, token)
    if certificate is None:
        return render(request, "missing.html", status_code=404)
    if repair_iiko_part_balance_drift(db, certificate):
        db.commit()
        db.refresh(certificate)
    base = public_base(request)
    # Ссылка сотруднику: без кнопок «Отменить», даже если в сессии HR/админ.
    card = load_card(db, certificate, base, None)
    reason = _unavailable_reason(certificate)
    is_unavailable = reason is not None
    if is_unavailable:
        # Штрихкод и номер не отдаём на страницу — иначе скрытие обходится через DevTools.
        card = replace(
            card,
            barcode_value="",
            barcode_svg="",
            public_number="MR-•••••",
            short_code="",
            unwrap_pending=False,
        )
    title, subtitle = _unavailable_copy(reason, card.first_name, certificate)
    wallet = (
        _empty_wallet_ui()
        if is_unavailable
        else _wallet_ui(db, certificate, base)
    )
    return render(
        request,
        "public_card.html",
        card=card,
        card_token=certificate.token,
        card_expired=is_unavailable,
        expired_title=title,
        expired_sub=subtitle,
        expires_on_iso=(
            certificate.expires_on.isoformat()
            if reason == "expired" and certificate.expires_on
            else ""
        ),
        **wallet,
        **_card_page_extras(),
    )


def _unavailable_reason(certificate: Certificate) -> str | None:
    """Почему карта недоступна: deleted / blocked / expired, иначе None."""
    if certificate.is_deleted:
        return "deleted"
    if certificate.status == CertificateStatus.blocked:
        return "blocked"
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return "expired"
    return None


def _unavailable_copy(
    reason: str | None,
    first_name: str,
    certificate: Certificate,
) -> tuple[str, str]:
    """Заголовок и подзаголовок экрана CardExpired."""
    who = f"{first_name}, карта" if first_name else "Карта"
    if reason == "deleted":
        return (
            "Карта удалена",
            f"{who} больше не действует, штрихкод скрыт.",
        )
    if reason == "blocked":
        return (
            "Карта деактивирована",
            f"{who} временно недоступна, штрихкод скрыт.",
        )
    if reason == "expired" and certificate.expires_on is not None:
        day = certificate.expires_on.strftime("%d.%m.%Y")
        return (
            "Срок действия карты закончился",
            f"{who} действовала до {day}. Теперь она неактивна, штрихкод скрыт.",
        )
    return (
        "Срок действия карты закончился",
        f"{who} больше не действует, штрихкод скрыт.",
    )


def _certificate_expired(certificate: Certificate) -> bool:
    """Карта недоступна: удалена, заблокирована или истёк срок."""
    return _unavailable_reason(certificate) is not None


def _empty_wallet_ui() -> dict:
    """Скрывает кнопки кошелька на экране истёкшей карты."""
    return {
        "apple_wallet_enabled": False,
        "apple_wallet_href": "",
        "walletpasses_enabled": False,
        "walletpasses_href": "",
    }


def _parse_before(raw: str) -> int | None:
    """Курсор before: целое > 0, иначе None (ответ 400)."""
    text = (raw or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value <= 0:
        return None
    return value


def _clamp_history_limit(raw: int) -> int:
    """Ограничивает limit порции: 1…50, по умолчанию HISTORY_PAGE_SIZE."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return HISTORY_PAGE_SIZE
    if value < 1:
        return 1
    if value > 50:
        return 50
    return value


def _history_fragment_response(
    request: Request,
    *,
    items: tuple[TxView, ...],
    csrf: str,
    has_more: bool,
    next_before: int | None,
) -> Response:
    """HTML <li> строк + заголовки X-Has-More / X-Next-Before."""
    response = templates.TemplateResponse(
        request,
        "_history_items.html",
        {
            "request": request,
            "items": items,
            "show_holder": False,
            "next_path": "",
            "csrf": csrf,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Has-More"] = "1" if has_more else "0"
    if next_before is not None:
        response.headers["X-Next-Before"] = str(next_before)
    return response


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


@router.get("/c/{token}/apple-wallet.pkpass")
def apple_wallet_pkpass(token: str, request: Request, db: Session = Depends(get_db)):
    """Скачивание .pkpass для Apple Wallet и WalletPasses (Android)."""
    certificate = find_certificate(db, token)
    if certificate is None:
        return render(request, "missing.html", status_code=404)
    if _certificate_expired(certificate):
        return RedirectResponse(f"/c/{certificate.token}", status_code=302)
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

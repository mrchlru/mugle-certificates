"""Тело pass для WalletWallet API."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import Certificate, User
from bonus.partners import partner_cities
from bonus.parts import list_parts
from bonus.present import _use_days_caption
from bonus.values import MSK, format_short_code, format_wallet_balance_rubles, today_msk
from bonus.wallet_assets import WalletBranding, prepare_wallet_branding
from bonus.wallet_context import resolve_wallet_card_context, wallet_holder_display_name

_WALLET_PASS_TITLE = "Карта"
_WALLET_MAX_EXPIRY_YEARS = 10


def build_walletwallet_body(
    db: Session,
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str,
    show_balance: bool,
    notify_on_change: bool,
    branding_pro: bool = True,
) -> dict[str, Any]:
    """Собирает JSON для POST/PUT /api/passes."""
    barcode_label = _barcode_label(certificate)
    if not barcode_label:
        raise ValueError("empty barcode")
    ctx = resolve_wallet_card_context(db, certificate)
    rubles = _display_balance_rubles(db, certificate)
    settings = get_settings()
    date_layout = _wallet_date_layout(settings)
    body: dict[str, Any] = {
        "organizationName": _organization_name(settings),
        "description": (settings.apple_pass_description or "Бонусная карта")[:64],
        "sharingProhibited": True,
        "title": _WALLET_PASS_TITLE,
        "barcodeValue": barcode_label[:1024],
        "barcodeFormat": "Code128",
        "barcodeAltText": barcode_label[:128],
        "secondaryFields": _secondary_fields(
            db,
            certificate,
            holder,
            ctx,
            date_layout=date_layout,
        ),
        "backFields": _back_fields(db, certificate, holder, base_url=base_url, show_balance=show_balance),
    }
    _apply_expiry_layout(body, certificate, date_layout=date_layout)
    _apply_branding(db, certificate, body, branding_pro=branding_pro)
    if show_balance and rubles >= 0:
        balance_field: dict[str, str] = {
            "label": "Баланс",
            "value": format_wallet_balance_rubles(rubles),
        }
        if notify_on_change:
            balance_field["changeMessage"] = "Баланс: %@"
        body["headerFields"] = [balance_field]
    exp = _expiration_rfc3339(certificate.expires_on)
    if exp:
        body["expirationDate"] = exp
    return body


def wallet_body_fingerprint(body: dict[str, Any]) -> str:
    """Хэш тела для пропуска лишних PUT."""
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _apply_branding(
    db: Session,
    certificate: Certificate,
    body: dict[str, Any],
    *,
    branding_pro: bool,
) -> None:
    """Pro: data URI картинок и цвет карты; иначе пресет."""
    if branding_pro:
        branding: WalletBranding | None = prepare_wallet_branding(db, certificate)
        if branding is not None:
            body["color"] = branding.color
            body["logoURL"] = branding.logo_data_uri
            body["stripURL"] = branding.strip_data_uri
            body["iconURL"] = branding.icon_data_uri
            return
    body["colorPreset"] = "green"


def _barcode_label(certificate: Certificate) -> str:
    """Код как на странице карты (например 7449-7853)."""
    return format_short_code(certificate.short_code or "")


def _display_balance_rubles(db: Session, certificate: Certificate) -> int:
    parts = list_parts(db, certificate.id)
    if parts:
        total = sum(max(0, int(part.balance)) for part in parts)
    else:
        total = max(0, int(certificate.balance))
    return total // 100


def _secondary_fields(
    db: Session,
    certificate: Certificate,
    holder: User | None,
    ctx,
    *,
    date_layout: str,
) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    owner = wallet_holder_display_name(holder)
    if owner:
        label = _owner_field_label(certificate, date_layout=date_layout)
        fields.append({"label": label, "value": owner})
    discount = ctx.discount_percent or int(certificate.discount_percent or 0)
    if discount:
        fields.append({"label": "Скидка", "value": f"{discount}%"})
    return fields


def _apply_expiry_layout(
    body: dict[str, Any],
    certificate: Certificate,
    *,
    date_layout: str,
) -> None:
    """Срок: footerFields (по умолчанию) или подпись у владельца."""
    if certificate.expires_on is None:
        return
    if date_layout != "footer":
        return
    body["footerFields"] = [
        {
            "label": "Действует до",
            "value": certificate.expires_on.strftime("%d.%m.%Y"),
        }
    ]


def _owner_field_label(certificate: Certificate, *, date_layout: str) -> str:
    if certificate.expires_on is None or date_layout != "owner_label":
        return "Владелец карты"
    until = certificate.expires_on.strftime("%d.%m.%Y")
    return f"Владелец карты · до {until}"


def _organization_name(settings) -> str:
    name = (settings.apple_organization_name or "Mugle Rest").strip()
    return name[:64] if name else "Mugle Rest"


def _wallet_date_layout(settings) -> str:
    layout = (settings.wallet_date_layout or "footer").strip().casefold()
    if layout == "owner_label":
        return "owner_label"
    return "footer"


def _back_fields(
    db: Session,
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str,
    show_balance: bool,
) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    fields.append(
        {
            "label": "Новости",
            "value": " ",
            "changeMessage": "%@",
        }
    )
    if not show_balance:
        root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
        token = (certificate.token or "").strip()
        card_url = f"{root}/c/{token}" if root and token else ""
        value = (
            f"Актуальный баланс — на странице карты: {card_url}"
            if card_url
            else "Актуальный баланс — на странице карты в Teams Bonus."
        )
        fields.append({"label": "Баланс", "value": value[:400]})
    note = (certificate.note or "").strip()
    if note:
        fields.append({"label": "Правила и условия", "value": note[:400]})
    when = _use_days_caption(certificate)
    if when:
        fields.append({"label": "Когда действует", "value": when[:200]})
    partners = _partners_blurb(base_url)
    if partners:
        fields.append({"label": "Где действует", "value": partners[:400]})
    settings = get_settings()
    contacts = _contact_blurb(settings)
    if contacts:
        fields.append({"label": "Контакты", "value": contacts[:300]})
    company = (certificate.company or "").strip()
    if company:
        fields.append({"label": "Юридическое лицо", "value": company[:200]})
    number = (certificate.public_number or "").strip()
    if number:
        fields.append({"label": "Номер карты", "value": number[:128]})
    return fields


def _partners_blurb(base_url: str) -> str:
    root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
    names: list[str] = []
    for city in partner_cities():
        for place in city.places:
            if place.name and place.name not in names:
                names.append(place.name)
            if len(names) >= 12:
                break
        if len(names) >= 12:
            break
    if names:
        return ", ".join(names)
    if root:
        return "Список ресторанов — в разделе «Партнёры» на странице карты."
    return "Список ресторанов-партнёров — на странице карты в браузере."


def _contact_blurb(settings) -> str:
    parts: list[str] = []
    support = (settings.support_telegram or "").strip()
    if support:
        if support.startswith("@"):
            parts.append(f"Telegram {support}")
        else:
            parts.append(support)
    public = (settings.app_public_url or "").strip()
    if public:
        parts.append(public)
    return " · ".join(parts)


def _expiration_rfc3339(expires_on: date | None) -> str:
    """Конец срока карты в UTC (23:59:59 МСК). WalletWallet: не дальше 10 лет."""
    if expires_on is None:
        return ""
    today = today_msk()
    if expires_on < today:
        return ""
    effective = min(expires_on, _walletwallet_max_expiry_date(today))
    end_local = datetime.combine(effective, time(23, 59, 59), tzinfo=MSK)
    return end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _walletwallet_max_expiry_date(today: date) -> date:
    """Верхняя граница expirationDate для API WalletWallet."""
    try:
        return today.replace(year=today.year + _WALLET_MAX_EXPIRY_YEARS)
    except ValueError:
        return today.replace(year=today.year + _WALLET_MAX_EXPIRY_YEARS, month=2, day=28)

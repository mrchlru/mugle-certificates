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
from bonus.pkpass_fields import (
    apply_walletwallet_api_minimum,
    apply_walletwallet_expiry,
    apply_walletwallet_inline_field_labels,
    apply_walletwallet_pass_colors,
    face_secondary_walletwallet,
    wallet_date_layout,
)
from bonus.pkpass_images import pkpass_organization_name
from bonus.wallet_assets import WalletBranding, prepare_wallet_branding
from bonus.wallet_context import resolve_wallet_card_context

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
    date_layout = wallet_date_layout(settings)
    body: dict[str, Any] = {
        "organizationName": pkpass_organization_name(settings),
        "description": (settings.apple_pass_description or "Бонусная карта")[:64],
        "sharingProhibited": True,
        "barcodeValue": barcode_label[:1024],
        "barcodeFormat": "Code128",
        "barcodeAltText": barcode_label[:128],
        "secondaryFields": face_secondary_walletwallet(
            certificate,
            holder,
            discount_percent=ctx.discount_percent,
            date_layout=date_layout,
        ),
        "backFields": _back_fields(db, certificate, holder, base_url=base_url, show_balance=show_balance),
    }
    apply_walletwallet_expiry(body, certificate, date_layout=date_layout)
    _apply_branding(db, certificate, body, branding_pro=branding_pro, base_url=base_url)
    apply_walletwallet_api_minimum(body)
    apply_walletwallet_pass_colors(body)
    apply_walletwallet_inline_field_labels(body)
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
    base_url: str,
) -> None:
    """Pro: data URI картинок и цвет карты; иначе пресет."""
    if branding_pro:
        branding: WalletBranding | None = prepare_wallet_branding(
            db,
            certificate,
            base_url=base_url,
        )
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

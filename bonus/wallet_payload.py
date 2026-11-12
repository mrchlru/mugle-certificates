"""Тело pass для WalletWallet API."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import Certificate, User
from bonus.parts import list_parts
from bonus.values import MSK, format_short_code, format_wallet_balance_rubles, today_msk
from bonus.wallet_assets import wallet_asset_urls, wallet_branding_ready
from bonus.weekdays import WEEKDAY_CHOICES

ORG_NAME = "Teams Bonus"
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
    name = (holder.full_name if holder is not None else "") or "Сотрудник"
    rubles = _display_balance_rubles(db, certificate)
    body: dict[str, Any] = {
        "organizationName": ORG_NAME[:64],
        "description": "Карта Teams Bonus",
        "sharingProhibited": True,
        "barcodeValue": barcode_label[:1024],
        "barcodeFormat": "Code128",
        "barcodeAltText": barcode_label[:128],
        "primaryFields": [{"label": "Владелец карты", "value": name}],
        "secondaryFields": _secondary_fields(certificate),
        "backFields": _back_fields(db, certificate, holder, base_url=base_url, show_balance=show_balance),
    }
    _apply_branding(body, base_url=base_url, branding_pro=branding_pro)
    if show_balance:
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


def _apply_branding(body: dict[str, Any], *, base_url: str, branding_pro: bool) -> None:
    """Pro: logoURL, stripURL, color; иначе пресет green и logoText."""
    use_pro = branding_pro and wallet_branding_ready(base_url)
    if use_pro:
        settings = get_settings()
        urls = wallet_asset_urls(base_url, settings)
        if urls:
            body["color"] = (settings.wallet_pass_color or "#00A8A8").strip()[:32]
            body["logoURL"] = urls["logo"]
            body["stripURL"] = urls["strip"]
            body["iconURL"] = urls["icon"]
            return
    body["logoText"] = ORG_NAME
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


def _secondary_fields(certificate: Certificate) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    segment = getattr(certificate, "segment", None)
    if segment is not None and getattr(segment, "name", None):
        fields.append({"label": "Тип", "value": str(segment.name).strip()})
    if certificate.expires_on is not None:
        fields.append(
            {"label": "Срок", "value": certificate.expires_on.strftime("%d.%m.%Y")},
        )
    if certificate.discount_percent:
        fields.append({"label": "Скидка", "value": f"{certificate.discount_percent}%"})
    return fields


def _back_fields(
    db: Session,
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str,
    show_balance: bool,
) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
    token = (certificate.token or "").strip()
    card_url = f"{root}/c/{token}" if root and token else ""
    if not show_balance:
        value = (
            f"Актуальный баланс — на странице карты: {card_url}"
            if card_url
            else "Актуальный баланс — на странице карты в Teams Bonus."
        )
        fields.append({"label": "Баланс", "value": value[:400]})
    number = (certificate.public_number or "").strip()
    if number:
        fields.append({"label": "Номер карты", "value": number[:128]})
    days_label = _use_days_label(certificate)
    if days_label:
        fields.append({"label": "Дни использования", "value": days_label})
    note = (certificate.note or "").strip()
    if note:
        fields.append({"label": "Правила", "value": note[:400]})
    company = (certificate.company or "").strip()
    if company:
        fields.append({"label": "Юрлицо", "value": company[:200]})
    settings = get_settings()
    support = (settings.support_telegram or "").strip()
    if support:
        fields.append({"label": "Поддержка", "value": support[:128]})
    fields.append(
        {
            "label": "Партнёры",
            "value": "Список ресторанов — на странице карты в браузере.",
        }
    )
    if card_url:
        fields.append({"label": "Карта в браузере", "value": card_url[:400]})
    return fields


def _use_days_label(certificate: Certificate) -> str:
    mask = (certificate.use_days or "1234567").strip()
    active = [label for digit, label in WEEKDAY_CHOICES if digit in mask]
    return ", ".join(active) if active else ""


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

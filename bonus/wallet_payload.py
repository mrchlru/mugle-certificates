"""Тело pass для WalletWallet API."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import Certificate, User
from bonus.parts import list_parts
from bonus.values import MSK, format_short_code, today_msk
from bonus.weekdays import WEEKDAY_CHOICES

ORG_NAME = "Teams Bonus"


def build_walletwallet_body(
    db: Session,
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str,
    show_balance: bool,
    notify_on_change: bool,
) -> dict[str, Any]:
    """Собирает JSON для POST/PUT /api/passes."""
    short = (certificate.short_code or "").replace("-", "").replace(" ", "")
    name = (holder.full_name if holder is not None else "") or "Сотрудник"
    rubles = _display_balance_rubles(db, certificate)
    body: dict[str, Any] = {
        "organizationName": ORG_NAME[:64],
        "logoText": ORG_NAME,
        "description": "Карта Teams Bonus",
        "colorPreset": "green",
        "sharingProhibited": True,
        "barcodeValue": short,
        "barcodeFormat": "Code128",
        "barcodeAltText": (certificate.public_number or format_short_code(short))[:128],
        "primaryFields": [{"label": "Владелец", "value": name}],
        "secondaryFields": _secondary_fields(certificate),
        "backFields": _back_fields(db, certificate, holder, base_url=base_url, show_balance=show_balance),
    }
    if show_balance:
        balance_field: dict[str, str] = {
            "label": "Баланс",
            "value": f"{rubles} ₽",
        }
        if notify_on_change:
            balance_field["changeMessage"] = "Баланс: %@"
        body["headerFields"] = [balance_field]
    exp = _expiration_rfc3339(certificate.expires_on)
    if exp:
        body["expirationDate"] = exp
    return body


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
    number = certificate.public_number or str(certificate.id)
    fields.append({"label": "Номер", "value": number})
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
    if not show_balance and card_url:
        fields.append(
            {
                "label": "Баланс",
                "value": f"Актуальный баланс — на странице карты: {card_url}",
            }
        )
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
        fields.append({"label": "Карта в браузере", "value": card_url})
    return fields


def _use_days_label(certificate: Certificate) -> str:
    mask = (certificate.use_days or "1234567").strip()
    active = [label for digit, label in WEEKDAY_CHOICES if digit in mask]
    return ", ".join(active) if active else ""


def _expiration_rfc3339(expires_on: date | None) -> str:
    """Конец срока карты в UTC (23:59:59 МСК в день expires_on)."""
    if expires_on is None:
        return ""
    if expires_on < today_msk():
        return ""
    end_local = datetime.combine(expires_on, time(23, 59, 59), tzinfo=MSK)
    return end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

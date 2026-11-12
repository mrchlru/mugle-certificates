"""Поля лицевой стороны .pkpass (WalletWallet JSON и Apple storeCard)."""

from __future__ import annotations

from typing import Any

from bonus.config import Settings, get_settings
from bonus.models import Certificate, User
from bonus.wallet_context import wallet_holder_display_name

PKPASS_API_TITLE = "Карта"


def wallet_date_layout(settings: Settings | None = None) -> str:
    """footer — WalletWallet footerFields; owner_label — дата в подписи владельца."""
    cfg = settings or get_settings()
    layout = (getattr(cfg, "wallet_date_layout", None) or "footer").strip().casefold()
    if layout == "owner_label":
        return "owner_label"
    return "footer"


def owner_field_label(certificate: Certificate, *, date_layout: str) -> str:
    if certificate.expires_on is None or date_layout != "owner_label":
        return "Владелец карты"
    until = certificate.expires_on.strftime("%d.%m.%Y")
    return f"Владелец карты · до {until}"


def face_secondary_walletwallet(
    certificate: Certificate,
    holder: User | None,
    *,
    discount_percent: int,
    date_layout: str,
) -> list[dict[str, str]]:
    """Владелец и скидка; без «Тип» и без отдельного «Срок» в строке."""
    fields: list[dict[str, str]] = []
    owner = wallet_holder_display_name(holder)
    if owner:
        fields.append(
            {
                "label": owner_field_label(certificate, date_layout=date_layout),
                "value": owner,
            }
        )
    discount = int(discount_percent or certificate.discount_percent or 0)
    if discount:
        fields.append({"label": "Скидка", "value": f"{discount}%"})
    return fields


def apply_walletwallet_expiry(
    body: dict[str, Any],
    certificate: Certificate,
    *,
    date_layout: str,
) -> None:
    if certificate.expires_on is None or date_layout != "footer":
        return
    body["footerFields"] = [
        {
            "label": "Действует до",
            "value": certificate.expires_on.strftime("%d.%m.%Y"),
        }
    ]


def face_store_card_apple(
    certificate: Certificate,
    holder: User | None,
    *,
    discount_percent: int,
    date_layout: str,
) -> dict[str, list[dict[str, str]]]:
    """storeCard: secondary + auxiliary (срок под владельцем в PassKit)."""
    secondary: list[dict[str, str]] = []
    auxiliary: list[dict[str, str]] = []
    owner = wallet_holder_display_name(holder)
    if owner:
        label = owner_field_label(certificate, date_layout=date_layout)
        secondary.append({"key": "holder", "label": label.upper(), "value": owner})
    discount = int(discount_percent or certificate.discount_percent or 0)
    if discount:
        secondary.append(
            {"key": "discount", "label": "СКИДКА", "value": f"{discount}%"},
        )
    if certificate.expires_on is not None and date_layout == "footer":
        auxiliary.append(
            {
                "key": "expires",
                "label": "ДЕЙСТВУЕТ ДО",
                "value": certificate.expires_on.strftime("%d.%m.%Y"),
            }
        )
    return {"secondaryFields": secondary, "auxiliaryFields": auxiliary}

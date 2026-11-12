"""Поля лицевой стороны .pkpass (WalletWallet JSON и Apple storeCard)."""

from __future__ import annotations

from typing import Any

from bonus.config import Settings, get_settings
from bonus.models import Certificate, User
from bonus.wallet_context import wallet_holder_display_name

# WalletWallet требует title | primaryFields | logoText; без видимого текста на карте.
WALLET_API_LOGO_TEXT = "\u200b"


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
    """footerFields на store card часто не виден — см. owner_label."""
    body.pop("footerFields", None)
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
    if certificate.expires_on is not None and date_layout == "owner_label":
        auxiliary.clear()
    return {"secondaryFields": secondary, "auxiliaryFields": auxiliary}


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    cleaned = (color or "").strip().lstrip("#")
    if len(cleaned) != 6:
        return 255, 90, 0
    return int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)


def _rgb_css(rgb: tuple[int, int, int]) -> str:
    return f"rgb({rgb[0]}, {rgb[1]}, {rgb[2]})"


_PRESET_BG = {
    "green": "#00A8A8",
    "blue": "#1D4ED8",
    "orange": "#FF5A00",
    "red": "#B91C1C",
    "purple": "#7C3AED",
    "dark": "#111111",
}

_HEX_TO_PRESET = {
    "#00A8A8": "green",
    "#FF5A00": "orange",
    "#B91C1C": "red",
    "#1D4ED8": "blue",
    "#111111": "dark",
    "#7C3AED": "purple",
}


def walletwallet_preset_for_hex(hex_color: str) -> str | None:
    """Пресет WalletWallet для типовых цветов карты (влияет на контраст текста)."""
    cleaned = (hex_color or "").strip().upper()
    if len(cleaned) == 7 and cleaned.startswith("#"):
        return _HEX_TO_PRESET.get(cleaned)
    return None


def resolve_walletwallet_background_hex(body: dict[str, Any]) -> str:
    """Фон pass из Pro color или colorPreset."""
    raw = (body.get("color") or "").strip()
    if raw.startswith("#") and len(raw) >= 7:
        return raw[:7].upper()
    preset = (body.get("colorPreset") or "green").strip().casefold()
    return _PRESET_BG.get(preset, "#00A8A8")


def apply_walletwallet_pass_colors(body: dict[str, Any]) -> None:
    """Белые подписи при сохранении Pro `color` (#hex) для точного фона.

    API WalletWallet задаёт фон через `color`, не `backgroundColor`; при снятии `color`
    уходит чёрный `dark`. Явные foreground/label в rgb дополняют пресет.
    """
    body.pop("backgroundColor", None)
    white = "rgb(255, 255, 255)"
    body["foregroundColor"] = white
    body["labelColor"] = white
    hex_bg = resolve_walletwallet_background_hex(body)
    preset = walletwallet_preset_for_hex(hex_bg)
    if preset:
        body["colorPreset"] = preset
    elif not body.get("color") and not body.get("colorPreset"):
        body["colorPreset"] = "green"


def apply_walletwallet_api_minimum(body: dict[str, Any]) -> None:
    """Минимум для API без «Карта» на баннере и в шапке."""
    body.pop("title", None)
    body["logoText"] = WALLET_API_LOGO_TEXT

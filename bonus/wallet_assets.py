"""Картинки pkpass для WalletWallet: data URI и кеш."""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from io import BytesIO

from PIL import Image
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import Certificate
from bonus.pkpass_images import (
    default_pkpass_logo,
    default_pkpass_strip,
    load_pkpass_media,
    png_icon_walletwallet,
    png_logo_walletwallet,
    png_strip_walletwallet,
)
from bonus.wallet_context import WalletCardContext, resolve_wallet_card_context

logger = logging.getLogger("bonus.wallet_assets")

_MAX_IMAGE_BYTES = 1_000_000


@dataclass(frozen=True)
class WalletBranding:
    """Pro-поля для JSON pass."""

    color: str
    logo_data_uri: str
    strip_data_uri: str
    icon_data_uri: str


def prepare_wallet_branding(
    db: Session,
    certificate: Certificate,
    *,
    base_url: str = "",
) -> WalletBranding | None:
    """Картинки pkpass из шаблона карты."""
    ctx = resolve_wallet_card_context(db, certificate)
    try:
        logo = _prepare_logo(ctx, base_url=base_url)
        strip = _prepare_strip(ctx.strip_source, base_url=base_url)
        icon = _prepare_icon(ctx, base_url=base_url)
        if not logo or not strip or not icon:
            return None
        return WalletBranding(
            color=ctx.color,
            logo_data_uri=_png_data_uri(logo),
            strip_data_uri=_png_data_uri(strip),
            icon_data_uri=_png_data_uri(icon),
        )
    except Exception as exc:
        logger.warning(
            "Wallet branding prepare failed cert=%s: %s",
            certificate.public_number or certificate.id,
            type(exc).__name__,
        )
        return None


def branding_source_digest(ctx: WalletCardContext) -> str:
    """Ключ для инвалидации кеша при смене оформления."""
    blob = "|".join(
        [
            ctx.color,
            ctx.strip_source,
            ctx.logo_source,
            str(ctx.logo_custom),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _prepare_logo(ctx: WalletCardContext, *, base_url: str) -> bytes:
    if ctx.logo_custom and ctx.logo_source:
        raw = load_pkpass_media(ctx.logo_source, base_url=base_url)
    else:
        raw = default_pkpass_logo(white=True)
    if raw is None:
        raw = default_pkpass_logo(white=True)
    kind = "logo_custom" if ctx.logo_custom else "logo_white"
    return _cached_process(raw, kind, png_logo_walletwallet)


def _prepare_strip(source: str, *, base_url: str) -> bytes:
    raw = load_pkpass_media(source, base_url=base_url) if source else None
    if raw is None and source:
        logger.warning("Wallet strip missing for source=%s", source[:120])
    if raw is None:
        raw = default_pkpass_strip()
    if raw is None:
        raise ValueError("default strip missing")
    return _cached_process(raw, "strip", png_strip_walletwallet)


def _prepare_icon(ctx: WalletCardContext, *, base_url: str) -> bytes:
    if ctx.logo_custom and ctx.logo_source:
        raw = load_pkpass_media(ctx.logo_source, base_url=base_url)
        kind = "icon"
    else:
        raw = default_pkpass_logo(white=True)
        kind = "icon_white"
    if raw is None:
        raw = default_pkpass_logo(white=True)
    return _cached_process(raw, kind, png_icon_walletwallet)


def _cached_process(raw: bytes, kind: str, transform) -> bytes:
    """Кеш по хэшу исходника в DATA_DIR/wallet-cache."""
    digest = hashlib.sha256(raw).hexdigest()
    cache_dir = get_settings().data_path() / "wallet-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{digest}_{kind}.png"
    if cache_path.is_file():
        data = cache_path.read_bytes()
        if len(data) <= _MAX_IMAGE_BYTES:
            return data
    png = _shrink_png(transform(raw))
    cache_path.write_bytes(png)
    return png


def _shrink_png(png: bytes) -> bytes:
    """Ужимает PNG до лимита 1 МБ."""
    if len(png) <= _MAX_IMAGE_BYTES:
        return png
    img = Image.open(BytesIO(png))
    scale = 0.85
    while scale >= 0.4:
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        smaller = img.resize((w, h), Image.Resampling.LANCZOS)
        buf = BytesIO()
        smaller.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= _MAX_IMAGE_BYTES:
            return data
        scale -= 0.15
    return png[:_MAX_IMAGE_BYTES]


def _png_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")

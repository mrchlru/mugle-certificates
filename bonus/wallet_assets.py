"""Подготовка картинок pass: resize, кеш, PNG data URI для WalletWallet."""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import Certificate
from bonus.storage import read_template_bytes
from bonus.wallet_context import WalletCardContext, resolve_wallet_card_context

logger = logging.getLogger("bonus.wallet_assets")

_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_DEFAULT_LOGO = _STATIC_ROOT / "wallet" / "logo.png"
_DEFAULT_STRIP = _STATIC_ROOT / "wallet" / "strip.png"
_DEFAULT_ICON = _STATIC_ROOT / "wallet" / "icon.png"

_LOGO_BOX = (480, 150)
_STRIP_BOX = (1125, 432)
_ICON_BOX = (114, 114)
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
) -> WalletBranding | None:
    """Картинки и цвет из карты; при ошибке — None (fallback на пресет)."""
    ctx = resolve_wallet_card_context(db, certificate)
    try:
        logo = _prepare_logo(ctx.logo_source)
        strip = _prepare_strip(ctx.strip_source)
        icon = _prepare_icon(ctx.logo_source if ctx.logo_custom else "")
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


def _prepare_logo(source: str) -> bytes:
    raw = _load_media_bytes(source) if source else _read_default(_DEFAULT_LOGO)
    if raw is None:
        raw = _read_default(_DEFAULT_LOGO)
    return _cached_process(raw, "logo", _fit_logo)


def _prepare_strip(source: str) -> bytes:
    raw = _load_media_bytes(source) if source else _read_default(_DEFAULT_STRIP)
    if raw is None:
        raw = _read_default(_DEFAULT_STRIP)
    return _cached_process(raw, "strip", _fit_strip)


def _prepare_icon(logo_source: str) -> bytes:
    raw = _load_media_bytes(logo_source) if logo_source else _read_default(_DEFAULT_ICON)
    if raw is None:
        raw = _read_default(_DEFAULT_ICON)
    return _cached_process(raw, "icon", _fit_icon)


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


def _fit_logo(raw: bytes) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGBA")
    contained = ImageOps.contain(img, _LOGO_BOX, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", _LOGO_BOX, (0, 0, 0, 0))
    ox = (_LOGO_BOX[0] - contained.width) // 2
    oy = (_LOGO_BOX[1] - contained.height) // 2
    canvas.paste(contained, (ox, oy), contained)
    return _image_to_png(canvas)


def _fit_strip(raw: bytes) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGB")
    fitted = ImageOps.fit(img, _STRIP_BOX, Image.Resampling.LANCZOS)
    return _image_to_png(fitted)


def _fit_icon(raw: bytes) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGBA")
    fitted = ImageOps.fit(img, _ICON_BOX, Image.Resampling.LANCZOS)
    return _image_to_png(fitted)


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


def _image_to_png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _png_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _read_default(path: Path) -> bytes | None:
    if path.is_file():
        return path.read_bytes()
    return None


def _load_media_bytes(source: str) -> bytes | None:
    """Локальный шаблон, /static/… или HTTP(S) CDN."""
    cleaned = (source or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        try:
            with httpx.Client(timeout=25.0, follow_redirects=True) as client:
                response = client.get(cleaned)
            if response.status_code < 400 and response.content:
                return response.content
        except httpx.HTTPError:
            return None
        return None
    if cleaned.startswith("/static/"):
        path = _STATIC_ROOT / cleaned.removeprefix("/static/").lstrip("/")
        if path.is_file():
            return path.read_bytes()
        return None
    raw = read_template_bytes(cleaned)
    if raw is not None:
        return raw
    if cleaned.lower().endswith((".svg",)):
        return None
    try:
        path = Path(cleaned)
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    return None

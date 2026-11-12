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
from bonus.storage import public_media_url, read_template_bytes
from bonus.wallet_context import WalletCardContext, resolve_wallet_card_context

logger = logging.getLogger("bonus.wallet_assets")

_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_DEFAULT_LOGO = _STATIC_ROOT / "wallet" / "logo.png"
_DEFAULT_STRIP = _STATIC_ROOT / "wallet" / "strip.png"
_DEFAULT_ICON = _STATIC_ROOT / "wallet" / "icon.png"
_LOGO_PAD_X = 12

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
        logo = _prepare_logo(ctx.logo_source, default_white=not ctx.logo_custom)
        strip = _prepare_strip(ctx.strip_source)
        icon = _prepare_icon(
            ctx.logo_source if ctx.logo_custom else "",
            default_white=not ctx.logo_custom,
        )
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


def _prepare_logo(source: str, *, default_white: bool) -> bytes:
    raw = _load_media_bytes(source) if source else _default_logo_raw(default_white=default_white)
    if raw is None:
        raw = _default_logo_raw(default_white=default_white)
    cache_kind = "logo_white" if default_white and not source else "logo"
    return _cached_process(raw, cache_kind, _fit_logo)


def _prepare_strip(source: str) -> bytes:
    raw = _load_media_bytes(source) if source else None
    if raw is None and source:
        logger.warning("Wallet strip missing for source=%s", source[:120])
    if raw is None:
        raw = _read_default(_DEFAULT_STRIP)
    if raw is None:
        raise ValueError("default strip missing")
    return _cached_process(raw, "strip", _fit_strip)


def _prepare_icon(logo_source: str, *, default_white: bool) -> bytes:
    if logo_source:
        raw = _load_media_bytes(logo_source)
    else:
        raw = _default_logo_raw(default_white=default_white)
    if raw is None:
        raw = _read_default(_DEFAULT_ICON)
    cache_kind = "icon_white" if default_white and not logo_source else "icon"
    return _cached_process(raw, cache_kind, _fit_icon)


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
    """Логотип слева в рамке 480×150 (как на странице карты)."""
    img = Image.open(BytesIO(raw)).convert("RGBA")
    pad = _LOGO_PAD_X
    inner = (_LOGO_BOX[0] - pad * 2, _LOGO_BOX[1])
    contained = ImageOps.contain(img, inner, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", _LOGO_BOX, (0, 0, 0, 0))
    ox = pad
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
    """Локальный шаблон, /static/…, /media/… или HTTP(S) CDN."""
    cleaned = (source or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        return _fetch_http_bytes(cleaned)
    if cleaned.startswith("/static/"):
        path = _STATIC_ROOT / cleaned.removeprefix("/static/").lstrip("/")
        if path.is_file():
            return path.read_bytes()
        return None
    if cleaned.startswith("/media/"):
        key = cleaned.removeprefix("/media/").lstrip("/")
        raw = read_template_bytes(key)
        if raw is not None:
            return raw
        url = public_media_url(key)
        if url.startswith(("http://", "https://")):
            return _fetch_http_bytes(url)
        return None
    raw = read_template_bytes(cleaned)
    if raw is not None:
        return raw
    if cleaned.lower().endswith((".svg",)):
        return None
    url = public_media_url(cleaned)
    if url.startswith(("http://", "https://")):
        fetched = _fetch_http_bytes(url)
        if fetched is not None:
            return fetched
    try:
        path = Path(cleaned)
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    return None


def _default_logo_raw(*, default_white: bool) -> bytes | None:
    """Дефолтный логотип как на странице карты (`/static/logo.svg` → PNG)."""
    for path in (_DEFAULT_LOGO, _STATIC_ROOT / "wallet-logo.png"):
        raw = _read_default(path)
        if raw is not None:
            return _logo_for_colored_pass(raw) if default_white else raw
    return None


def _logo_for_colored_pass(raw: bytes) -> bytes:
    """Белый логотип на прозрачном фоне (как filter: invert на карточке)."""
    img = Image.open(BytesIO(raw)).convert("RGBA")
    pixels = img.load()
    for y in range(img.height):
        for x in range(img.width):
            red, green, blue, alpha = pixels[x, y]
            if alpha < 8:
                continue
            pixels[x, y] = (255, 255, 255, alpha)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _fetch_http_bytes(url: str) -> bytes | None:
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "TeamsBonus-Wallet/1.0"})
        if response.status_code < 400 and response.content:
            return response.content
    except httpx.HTTPError:
        return None
    return None

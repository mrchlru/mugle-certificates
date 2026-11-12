"""Картинки для .pkpass: шаблон, CDN, дефолты (не страница карты в браузере)."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from bonus.config import Settings, get_settings
from bonus.storage import public_media_url, read_template_bytes

logger = logging.getLogger("bonus.pkpass_images")

_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_DEFAULT_LOGO = _STATIC_ROOT / "wallet" / "logo.png"
_DEFAULT_STRIP = _STATIC_ROOT / "wallet" / "strip.png"
_LOGO_PAD_X = 12


def load_pkpass_media(source: str, *, base_url: str = "") -> bytes | None:
    """Байты обложки/логотипа шаблона: диск, /media, CDN."""
    cleaned = (source or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        return _fetch_http(cleaned)
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
            return _fetch_http(url)
        root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
        if root:
            return _fetch_http(f"{root}{url}")
        return None
    raw = read_template_bytes(cleaned)
    if raw is not None:
        return raw
    if cleaned.lower().endswith(".svg"):
        return None
    url = public_media_url(cleaned)
    if url.startswith(("http://", "https://")):
        fetched = _fetch_http(url)
        if fetched is not None:
            return fetched
    if url.startswith("/"):
        root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
        if root:
            return _fetch_http(f"{root}{url}")
    try:
        path = Path(cleaned)
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    return None


def default_pkpass_logo(*, white: bool = True) -> bytes | None:
    """Дефолтный Mugle-логотип для цветного pass (белый на прозрачном)."""
    raw = _read_file(_DEFAULT_LOGO) or _read_file(_STATIC_ROOT / "wallet-logo.png")
    if raw is None:
        return None
    return _logo_white_on_transparent(raw) if white else raw


def default_pkpass_strip() -> bytes | None:
    return _read_file(_DEFAULT_STRIP)


def png_logo_walletwallet(raw: bytes) -> bytes:
    """480×150, логотип слева — формат WalletWallet."""
    box = (480, 150)
    return _png_logo_left(raw, box)


def png_strip_walletwallet(raw: bytes) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGB")
    fitted = ImageOps.fit(img, (1125, 432), Image.Resampling.LANCZOS)
    return _to_png(fitted)


def png_icon_walletwallet(raw: bytes) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGBA")
    fitted = ImageOps.fit(img, (114, 114), Image.Resampling.LANCZOS)
    return _to_png(fitted)


def png_logo_apple(raw: bytes, size: tuple[int, int]) -> bytes:
    return _png_logo_left(raw, size, contain=True)


def png_strip_apple(raw: bytes, size: tuple[int, int]) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGBA")
    return _to_png(_cover(img, size[0], size[1]))


def png_icon_apple(raw: bytes, size: tuple[int, int]) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGBA")
    fitted = ImageOps.fit(img, size, Image.Resampling.LANCZOS)
    return _to_png(fitted)


def _logo_white_on_transparent(raw: bytes) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGBA")
    pixels = img.load()
    for y in range(img.height):
        for x in range(img.width):
            _r, _g, _b, alpha = pixels[x, y]
            if alpha < 8:
                continue
            pixels[x, y] = (255, 255, 255, alpha)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _png_logo_left(raw: bytes, box: tuple[int, int], *, contain: bool = True) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGBA")
    pad = _LOGO_PAD_X
    inner = (box[0] - pad * 2, box[1])
    if contain:
        fitted = ImageOps.contain(img, inner, Image.Resampling.LANCZOS)
    else:
        fitted = img.resize(inner, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", box, (0, 0, 0, 0))
    ox = pad
    oy = (box[1] - fitted.height) // 2
    canvas.paste(fitted, (ox, oy), fitted)
    return _to_png(canvas)


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = image.size
    scale = max(width / src_w, height / src_h)
    resized = image.resize((int(src_w * scale), int(src_h * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _fetch_http(url: str) -> bytes | None:
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "TeamsBonus-pkpass/1.0"})
        if response.status_code < 400 and response.content:
            return response.content
    except httpx.HTTPError:
        logger.warning("pkpass image fetch failed: %s", url[:120])
    return None


def _read_file(path: Path) -> bytes | None:
    if path.is_file():
        return path.read_bytes()
    return None


def _to_png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def pkpass_organization_name(settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    name = (cfg.apple_organization_name or "Mugle Rest").strip()
    return name[:64] if name else "Mugle Rest"

"""Apple Wallet .pkpass и ссылка WalletPasses для Android."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from PIL import Image

from bonus.catalog import template_logo_is_custom
from bonus.config import Settings, get_settings
from bonus.models import Certificate, User
from bonus.pkpass_fields import face_store_card_apple, wallet_date_layout
from bonus.pkpass_images import (
    default_pkpass_logo,
    default_pkpass_strip,
    load_pkpass_media,
    pkpass_organization_name,
    png_icon_apple,
    png_logo_apple,
    png_strip_apple,
)
from bonus.values import format_short_code, format_wallet_balance_rubles
from bonus.wallet_payload import _expiration_rfc3339

logger = logging.getLogger("bonus.apple_wallet")

WALLETPASSES_IMPORT = "https://walletpass.io"
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def apple_wallet_enabled(settings: Settings | None = None) -> bool:
    """Включён ли выпуск .pkpass (нужны Pass Type ID, Team ID и сертификат)."""
    cfg = settings or get_settings()
    if not cfg.apple_wallet_enabled:
        return False
    if not cfg.apple_pass_type_id.strip() or not cfg.apple_team_id.strip():
        return False
    return _has_signing_material(cfg)


def build_pkpass(
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str = "",
) -> bytes | None:
    """Собирает подписанный .pkpass из шаблона админки или None при ошибке."""
    if not apple_wallet_enabled():
        return None
    try:
        settings = get_settings()
        files = _pass_files(certificate, holder, base_url=base_url, settings=settings)
        manifest = {
            name: hashlib.sha1(payload).hexdigest() for name, payload in files.items()
        }
        manifest_bytes = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        signature = _sign_manifest(manifest_bytes, settings)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in files.items():
                archive.writestr(name, payload)
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr("signature", signature)
        return buffer.getvalue()
    except Exception:
        logger.exception(
            "Apple Wallet pkpass failed for %s",
            certificate.public_number or certificate.id,
        )
        return None


def walletpasses_url(pkpass_absolute_url: str) -> str:
    """Ссылка Add to Wallet Passes (Android): импорт .pkpass через walletpass.io."""
    encoded = quote(pkpass_absolute_url, safe="")
    return f"{WALLETPASSES_IMPORT}?u={encoded}"


def pkpass_download_url(certificate: Certificate, base_url: str) -> str:
    """Абсолютный HTTPS URL файла .pkpass для WalletPasses / Apple."""
    root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
    token = (certificate.token or "").strip()
    if not root or not token:
        return ""
    return f"{root}/c/{token}/apple-wallet.pkpass"


def log_apple_wallet_status() -> None:
    """Пишет в лог старта, готов ли Apple / WalletPasses."""
    settings = get_settings()
    if not settings.apple_wallet_enabled:
        print("Teams Bonus: Apple Wallet выключен — APPLE_WALLET_ENABLED=false", flush=True)
        return
    if not settings.apple_pass_type_id.strip() or not settings.apple_team_id.strip():
        print(
            "Teams Bonus: Apple Wallet выключен — нет APPLE_PASS_TYPE_ID / APPLE_TEAM_ID",
            flush=True,
        )
        return
    if not _has_signing_material(settings):
        print(
            "Teams Bonus: Apple Wallet выключен — нет сертификата подписи "
            "(APPLE_PASS_CERT_PEM / P12)",
            flush=True,
        )
        return
    print(
        f"Teams Bonus: Apple Wallet OK passType={settings.apple_pass_type_id.strip()} "
        f"team={settings.apple_team_id.strip()}",
        flush=True,
    )


def _pass_files(
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str,
    settings: Settings,
) -> dict[str, bytes]:
    """Файлы внутри pkpass без manifest/signature."""
    pass_json = _build_pass_json(certificate, holder, base_url=base_url, settings=settings)
    images = _pass_images(certificate, base_url=base_url)
    files: dict[str, bytes] = {
        "pass.json": json.dumps(pass_json, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
    }
    files.update(images)
    return files


def _build_pass_json(
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str,
    settings: Settings,
) -> dict[str, Any]:
    """Собирает pass.json storeCard для .pkpass."""
    template = getattr(certificate, "template", None)
    description = (settings.apple_pass_description or "Бонусная карта").strip() or "Бонусная карта"
    org = pkpass_organization_name(settings)
    color = _normalize_hex(certificate.color or getattr(template, "color", None) or "#00A8A8")
    rgb = _hex_to_rgb(color)
    rubles = max(0, int(certificate.balance)) // 100
    short = (certificate.short_code or "").replace("-", "").replace(" ", "")
    serial = certificate.public_number or str(certificate.id)
    discount = int(getattr(certificate, "discount_percent", 0) or 0)
    if template is not None and getattr(template, "discount_percent", None):
        discount = int(template.discount_percent)
    date_layout = wallet_date_layout(settings)
    face = face_store_card_apple(
        certificate,
        holder,
        discount_percent=discount,
        date_layout=date_layout,
    )
    store: dict[str, Any] = {
        "headerFields": [
            {
                "key": "balance",
                "label": "БАЛАНС",
                "value": format_wallet_balance_rubles(rubles),
            }
        ],
        "backFields": _back_fields(certificate, base_url),
    }
    store.update(face)
    body: dict[str, Any] = {
        "formatVersion": 1,
        "passTypeIdentifier": settings.apple_pass_type_id.strip(),
        "serialNumber": serial,
        "teamIdentifier": settings.apple_team_id.strip(),
        "organizationName": org,
        "description": description,
        "foregroundColor": "rgb(255, 255, 255)",
        "backgroundColor": f"rgb({rgb[0]}, {rgb[1]}, {rgb[2]})",
        "labelColor": "rgb(255, 255, 255)",
        "barcode": {
            "format": "PKBarcodeFormatCode128",
            "message": short,
            "messageEncoding": "iso-8859-1",
            "altText": format_short_code(certificate.short_code or short),
        },
        "barcodes": [
            {
                "format": "PKBarcodeFormatCode128",
                "message": short,
                "messageEncoding": "iso-8859-1",
                "altText": format_short_code(certificate.short_code or short),
            }
        ],
        "storeCard": store,
    }
    exp = _expiration_rfc3339(certificate.expires_on)
    if exp:
        body["expirationDate"] = exp
    web = (settings.apple_pass_web_service_url or "").strip().rstrip("/")
    auth = (settings.apple_pass_auth_token or "").strip()
    if web and auth:
        body["webServiceURL"] = web
        body["authenticationToken"] = auth
    return body


def _back_fields(certificate: Certificate, base_url: str) -> list[dict[str, str]]:
    fields = [
        {
            "key": "number",
            "label": "Номер карты",
            "value": certificate.public_number or str(certificate.id),
        }
    ]
    if certificate.expires_on is not None:
        fields.append(
            {
                "key": "expires",
                "label": "Действует до",
                "value": certificate.expires_on.strftime("%d.%m.%Y"),
            }
        )
    root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
    token = (certificate.token or "").strip()
    if root and token:
        fields.append(
            {
                "key": "web",
                "label": "В браузере",
                "value": f"{root}/c/{token}",
            }
        )
    return fields


def _pass_images(certificate: Certificate, *, base_url: str) -> dict[str, bytes]:
    """icon/logo/strip внутри .pkpass."""
    template = getattr(certificate, "template", None)
    custom_logo = template_logo_is_custom(template)
    logo_path = (getattr(template, "logo_path", None) or "") if custom_logo else ""
    strip_path = (getattr(template, "image_path", None) or "") if template else ""
    if custom_logo and logo_path:
        logo_raw = load_pkpass_media(logo_path, base_url=base_url) or default_pkpass_logo(white=False)
    else:
        logo_raw = default_pkpass_logo(white=True)
    strip_raw = load_pkpass_media(strip_path, base_url=base_url) if strip_path else default_pkpass_strip()
    logo_raw = logo_raw or default_pkpass_logo(white=not custom_logo)
    icon = png_icon_apple(logo_raw, (58, 58))
    icon2x = png_icon_apple(logo_raw, (87, 87))
    logo_img = png_logo_apple(logo_raw, (160, 50))
    logo2x = png_logo_apple(logo_raw, (320, 100))
    images: dict[str, bytes] = {
        "icon.png": icon,
        "icon@2x.png": icon2x,
        "logo.png": logo_img,
        "logo@2x.png": logo2x,
    }
    if strip_raw:
        images["strip.png"] = png_strip_apple(strip_raw, (375, 123))
        images["strip@2x.png"] = png_strip_apple(strip_raw, (750, 246))
    return images


def _resize_png(
    payload: bytes,
    size: tuple[int, int],
    *,
    contain: bool = False,
    cover: bool = False,
) -> bytes:
    image = Image.open(io.BytesIO(payload)).convert("RGBA")
    width, height = size
    if cover:
        image = _cover(image, width, height)
    elif contain:
        image.thumbnail(size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        offset = ((width - image.width) // 2, (height - image.height) // 2)
        canvas.paste(image, offset, image)
        image = canvas
    else:
        image = image.resize(size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = image.size
    scale = max(width / src_w, height / src_h)
    resized = image.resize((int(src_w * scale), int(src_h * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _sign_manifest(manifest_bytes: bytes, settings: Settings) -> bytes:
    """PKCS#7 detached signature для Apple Wallet."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7

    cert, key = _load_signer(settings)
    wwdr = _load_wwdr(settings)
    builder = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(manifest_bytes)
        .add_signer(cert, key, hashes.SHA256())
    )
    if wwdr is not None:
        builder = builder.add_certificate(wwdr)
    return builder.sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature])


def _load_signer(settings: Settings) -> tuple[Any, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    p12_path = settings.apple_pass_p12_file.strip()
    if p12_path and Path(p12_path).is_file():
        data = Path(p12_path).read_bytes()
        password = settings.apple_pass_p12_password.encode("utf-8") or None
        key, cert, _chain = serialization.pkcs12.load_key_and_certificates(data, password)
        if key is None or cert is None:
            raise RuntimeError("P12 без ключа или сертификата Pass Type ID")
        return cert, key
    cert_pem = settings.apple_pass_cert_pem.strip()
    key_pem = settings.apple_pass_key_pem.strip()
    if not cert_pem and settings.apple_pass_cert_file.strip():
        cert_pem = Path(settings.apple_pass_cert_file.strip()).read_text(encoding="utf-8")
    if not key_pem and settings.apple_pass_key_file.strip():
        key_pem = Path(settings.apple_pass_key_file.strip()).read_text(encoding="utf-8")
    if not cert_pem or not key_pem:
        raise RuntimeError("Нет APPLE_PASS_CERT_PEM / APPLE_PASS_KEY_PEM или P12")
    cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
    key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
    return cert, key


def _load_wwdr(settings: Settings) -> Any | None:
    from cryptography import x509

    pem = settings.apple_wwdr_pem.strip()
    if not pem and settings.apple_wwdr_file.strip():
        path = Path(settings.apple_wwdr_file.strip())
        if path.is_file():
            pem = path.read_text(encoding="utf-8")
    if not pem:
        return None
    return x509.load_pem_x509_certificate(pem.encode("utf-8"))


def _has_signing_material(settings: Settings) -> bool:
    if settings.apple_pass_p12_file.strip() and Path(settings.apple_pass_p12_file.strip()).is_file():
        return True
    if settings.apple_pass_cert_pem.strip() and settings.apple_pass_key_pem.strip():
        return True
    cert_file = settings.apple_pass_cert_file.strip()
    key_file = settings.apple_pass_key_file.strip()
    return bool(cert_file and key_file and Path(cert_file).is_file() and Path(key_file).is_file())


def _normalize_hex(color: str) -> str:
    value = (color or "").strip()
    if len(value) == 7 and value.startswith("#"):
        return value.upper()
    if len(value) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in value):
        return f"#{value.upper()}"
    return "#00B596"


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = _normalize_hex(color).lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

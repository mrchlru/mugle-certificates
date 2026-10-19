"""Google Wallet Loyalty: class/object и ссылка Add to Google Wallet."""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jwt
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from bonus.config import Settings, get_settings
from bonus.models import Certificate, CertificateStatus, User
from bonus.values import MSK, format_short_code, today_msk

logger = logging.getLogger("bonus.google_wallet")

WALLET_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"
WALLET_API = "https://walletobjects.googleapis.com/walletobjects/v1"
SAVE_BASE = "https://pay.google.com/gp/v/save"

_class_ready: set[str] = set()
_class_lock = threading.Lock()


def wallet_enabled(settings: Settings | None = None) -> bool:
    """Включён ли выпуск карт в Google Wallet."""
    cfg = settings or get_settings()
    if not cfg.google_wallet_enabled:
        return False
    if not cfg.google_wallet_issuer_id.strip():
        return False
    return _has_wallet_credentials(cfg)


def class_id(settings: Settings | None = None) -> str:
    """Полный id Loyalty Class: `{issuerId}.{suffix}`."""
    cfg = settings or get_settings()
    suffix = (cfg.google_wallet_class_suffix or "teams_bonus").strip() or "teams_bonus"
    return f"{cfg.google_wallet_issuer_id.strip()}.{suffix}"


def object_id(certificate: Certificate, settings: Settings | None = None) -> str:
    """Стабильный id Loyalty Object для сертификата."""
    cfg = settings or get_settings()
    return f"{cfg.google_wallet_issuer_id.strip()}.cert_{certificate.id}"


def sync_certificate(
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str = "",
    expired: bool = False,
) -> None:
    """Создаёт или обновляет Loyalty Object. Ошибки только в лог."""
    if not wallet_enabled():
        return
    try:
        _ensure_loyalty_class(base_url=base_url, certificate=certificate)
        payload = build_loyalty_object(
            certificate,
            holder,
            base_url=base_url,
            expired=expired,
        )
        _upsert_loyalty_object(payload)
    except Exception:
        logger.exception(
            "Google Wallet sync failed for %s",
            certificate.public_number or certificate.id,
        )


def save_url(
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str,
    expired: bool = False,
) -> str | None:
    """Ссылка Add to Google Wallet (JWT) или None, если Wallet выключен."""
    if not wallet_enabled():
        return None
    try:
        class_payload = _ensure_loyalty_class(base_url=base_url, certificate=certificate)
        payload = build_loyalty_object(
            certificate,
            holder,
            base_url=base_url,
            expired=expired,
        )
        try:
            _upsert_loyalty_object(payload)
        except Exception:
            # JWT с полным object всё равно можно отдать — Google создаст при сохранении.
            logger.exception(
                "Google Wallet object upsert failed for %s; выдаём JWT без REST",
                certificate.public_number or certificate.id,
            )
        token = _signed_save_jwt(
            payload,
            loyalty_class=class_payload,
            origins=_origins(base_url),
        )
        return f"{SAVE_BASE}/{token}"
    except Exception:
        logger.exception(
            "Google Wallet save URL failed for %s",
            certificate.public_number or certificate.id,
        )
        return None


def build_loyalty_object(
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str = "",
    expired: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Собирает тело Loyalty Object без обращения к API."""
    cfg = settings or get_settings()
    rubles = max(0, int(certificate.balance)) // 100
    short = (certificate.short_code or "").replace("-", "").replace(" ", "")
    name = (holder.full_name if holder is not None else "") or "Сотрудник"
    color = _normalize_hex(certificate.color or "#00A8A8")
    segment = _segment_title(certificate)
    if segment:
        name = f"{name}, {segment.lower()}"
    body: dict[str, Any] = {
        "id": object_id(certificate, cfg),
        "classId": class_id(cfg),
        "state": _object_state(certificate, expired=expired),
        "accountId": certificate.public_number or str(certificate.id),
        "accountName": name,
        "loyaltyPoints": {
            "label": "Баланс",
            "balance": {"string": f"{rubles} ₽"},
        },
        "barcode": {
            "type": "CODE_128",
            "value": short,
            "alternateText": format_short_code(short),
        },
        "hexBackgroundColor": color,
        "textModulesData": _text_modules(certificate),
    }
    hero = _hero_image(certificate, base_url)
    if hero:
        body["heroImage"] = hero
    if certificate.expires_on is not None:
        body["validTimeInterval"] = {
            "end": {"date": _end_of_day_iso(certificate.expires_on)},
        }
    card_url = _card_url(certificate, base_url)
    if card_url:
        body["linksModuleData"] = {
            "uris": [
                {
                    "uri": card_url,
                    "description": "Открыть в браузере",
                    "id": "web_card",
                }
            ]
        }
    return body


def build_loyalty_class(
    *,
    base_url: str = "",
    certificate: Certificate | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Собирает тело Loyalty Class (бренд программы, не дублировать имена)."""
    cfg = settings or get_settings()
    logo = _logo_url(base_url, cfg, certificate=certificate)
    body: dict[str, Any] = {
        "id": class_id(cfg),
        "issuerName": (cfg.google_wallet_issuer_name or "Mugle Rest").strip(),
        "programName": (cfg.google_wallet_program_name or "Бонусная карта").strip(),
        "accountNameLabel": "Владелец",
        "accountIdLabel": "Номер карты",
        "reviewStatus": "UNDER_REVIEW",
        # Цвет class общий; индивидуальный цвет карты — на Loyalty Object.
        "hexBackgroundColor": "#00A8A8",
    }
    if logo:
        body["programLogo"] = {
            "sourceUri": {"uri": logo},
            "contentDescription": {
                "defaultValue": {"language": "ru", "value": "Logo"},
            },
        }
    return body


def log_wallet_status() -> None:
    """Пишет в лог старта, включён ли Google Wallet."""
    settings = get_settings()
    if not settings.google_wallet_enabled:
        print("Teams Bonus: Google Wallet выключен — GOOGLE_WALLET_ENABLED=false", flush=True)
        return
    if not settings.google_wallet_issuer_id.strip():
        print("Teams Bonus: Google Wallet выключен — нет GOOGLE_WALLET_ISSUER_ID", flush=True)
        return
    if not _has_wallet_credentials(settings):
        print(
            "Teams Bonus: Google Wallet выключен — нет GOOGLE_WALLET_SERVICE_ACCOUNT_JSON / FILE",
            flush=True,
        )
        return
    try:
        info = _wallet_service_account_info(settings)
        email = str(info.get("client_email") or "").strip()
        print(
            f"Teams Bonus: Google Wallet SA={email} "
            f"issuer={settings.google_wallet_issuer_id.strip()} "
            f"class={class_id(settings)}",
            flush=True,
        )
    except Exception as exc:
        print(f"Teams Bonus: Google Wallet проверка не удалась: {exc}", flush=True)


def _ensure_loyalty_class(
    *,
    base_url: str = "",
    certificate: Certificate | None = None,
) -> dict[str, Any]:
    """Создаёт/обновляет Loyalty Class (бренд + UNDER_REVIEW) и возвращает payload."""
    cfg = get_settings()
    cid = class_id(cfg)
    payload = build_loyalty_class(base_url=base_url, certificate=certificate, settings=cfg)
    with _class_lock:
        session = _authorized_session()
        existing = session.get(f"{WALLET_API}/loyaltyClass/{cid}")
        if existing.status_code == 200:
            _sync_class_branding(session, cid, payload, existing.json())
            _class_ready.add(cid)
            return payload
        created = session.post(f"{WALLET_API}/loyaltyClass", json=payload)
        if created.status_code in {200, 409}:
            if created.status_code == 409:
                again = session.get(f"{WALLET_API}/loyaltyClass/{cid}")
                if again.status_code == 200:
                    _sync_class_branding(session, cid, payload, again.json())
            _class_ready.add(cid)
            return payload
        raise RuntimeError(
            f"loyaltyClass create failed HTTP {created.status_code}: {created.text[:500]}"
        )


def _sync_class_branding(
    session: AuthorizedSession,
    cid: str,
    desired: dict[str, Any],
    current: dict[str, Any],
) -> None:
    """Обновляет оформление class и выводит из DRAFT."""
    patch: dict[str, Any] = {}
    for key in (
        "issuerName",
        "programName",
        "accountNameLabel",
        "accountIdLabel",
        "hexBackgroundColor",
        "programLogo",
    ):
        if key in desired and desired.get(key) != current.get(key):
            patch[key] = desired[key]
    status = str(current.get("reviewStatus") or "").upper()
    if status not in {"UNDER_REVIEW", "APPROVED"}:
        patch["reviewStatus"] = "UNDER_REVIEW"
    if not patch:
        return
    patched = session.patch(f"{WALLET_API}/loyaltyClass/{cid}", json=patch)
    if patched.status_code != 200:
        raise RuntimeError(
            f"loyaltyClass branding failed HTTP {patched.status_code}: {patched.text[:500]}"
        )


def _upsert_loyalty_object(payload: dict[str, Any]) -> None:
    """Создаёт Object или обновляет существующий."""
    oid = str(payload["id"])
    session = _authorized_session()
    existing = session.get(f"{WALLET_API}/loyaltyObject/{oid}")
    if existing.status_code == 200:
        updated = session.put(f"{WALLET_API}/loyaltyObject/{oid}", json=payload)
        if updated.status_code != 200:
            raise RuntimeError(
                f"loyaltyObject update failed HTTP {updated.status_code}: {updated.text[:500]}"
            )
        return
    created = session.post(f"{WALLET_API}/loyaltyObject", json=payload)
    if created.status_code in {200, 409}:
        if created.status_code == 409:
            updated = session.put(f"{WALLET_API}/loyaltyObject/{oid}", json=payload)
            if updated.status_code != 200:
                raise RuntimeError(
                    f"loyaltyObject update after 409 failed HTTP {updated.status_code}: "
                    f"{updated.text[:500]}"
                )
        return
    raise RuntimeError(
        f"loyaltyObject create failed HTTP {created.status_code}: {created.text[:500]}"
    )


def _signed_save_jwt(
    loyalty_object: dict[str, Any],
    *,
    loyalty_class: dict[str, Any] | None = None,
    origins: list[str],
) -> str:
    """Подписывает JWT для кнопки Add to Google Wallet."""
    settings = get_settings()
    info = _wallet_service_account_info(settings)
    email = str(info.get("client_email") or "").strip()
    private_key = str(info.get("private_key") or "")
    payload: dict[str, Any] = {"loyaltyObjects": [loyalty_object]}
    if loyalty_class is not None:
        payload["loyaltyClasses"] = [loyalty_class]
    claims = {
        "iss": email,
        "aud": "google",
        "typ": "savetowallet",
        "iat": int(time.time()),
        "origins": origins,
        "payload": payload,
    }
    return jwt.encode(claims, private_key, algorithm="RS256")


def _object_state(certificate: Certificate, *, expired: bool) -> str:
    if expired:
        return "EXPIRED"
    if certificate.status != CertificateStatus.active:
        return "INACTIVE"
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return "EXPIRED"
    return "ACTIVE"


def _text_modules(certificate: Certificate) -> list[dict[str, str]]:
    """Доп. поля под штрихкодом: код кассы, сегмент, срок — без дубля номера."""
    modules: list[dict[str, str]] = []
    short = (certificate.short_code or "").replace("-", "").replace(" ", "")
    if short:
        modules.append(
            {
                "id": "cashier_code",
                "header": "Код для кассы",
                "body": format_short_code(short),
            }
        )
    segment = _segment_title(certificate)
    if segment:
        modules.append({"id": "segment", "header": "Тип карты", "body": segment})
    if certificate.expires_on is not None:
        modules.append(
            {
                "id": "expires",
                "header": "Действует до",
                "body": certificate.expires_on.strftime("%d.%m.%Y"),
            }
        )
    if certificate.discount_percent:
        modules.append(
            {
                "id": "discount",
                "header": "Скидка",
                "body": f"{certificate.discount_percent}%",
            }
        )
    return modules


def _segment_title(certificate: Certificate) -> str:
    segment = getattr(certificate, "segment", None)
    if segment is not None and getattr(segment, "name", None):
        return str(segment.name).strip()
    return ""


def _hero_image(certificate: Certificate, base_url: str) -> dict[str, Any] | None:
    """Баннер шаблона как heroImage, если есть публичный HTTPS URL."""
    template = getattr(certificate, "template", None)
    path = ""
    if template is not None:
        path = (getattr(template, "image_path", None) or "").strip()
    if not path:
        return None
    from bonus.storage import public_media_url

    url = _absolute_https(public_media_url(path), base_url)
    if not url:
        return None
    return {
        "sourceUri": {"uri": url},
        "contentDescription": {
            "defaultValue": {"language": "ru", "value": "Обложка карты"},
        },
    }


def _end_of_day_iso(day: date) -> str:
    moment = datetime.combine(day, dt_time(23, 59, 59), tzinfo=MSK)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _card_url(certificate: Certificate, base_url: str) -> str:
    root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
    token = (certificate.token or "").strip()
    if not root or not token:
        return ""
    return f"{root}/c/{token}"


def _logo_url(
    base_url: str,
    settings: Settings,
    *,
    certificate: Certificate | None = None,
) -> str:
    configured = settings.google_wallet_logo_url.strip()
    if configured:
        return configured
    template = getattr(certificate, "template", None) if certificate is not None else None
    logo_path = ""
    if template is not None:
        logo_path = (getattr(template, "logo_path", None) or "").strip()
    if logo_path:
        from bonus.storage import public_media_url

        custom = _absolute_https(public_media_url(logo_path), base_url)
        if custom:
            return custom
    root = (base_url or settings.app_public_url or "").strip().rstrip("/")
    if not root:
        return ""
    return f"{root}/static/logo-teams.png"


def _absolute_https(url: str, base_url: str) -> str:
    """Google Wallet принимает только абсолютные https-картинки."""
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("https://"):
        return value
    if value.startswith("http://"):
        return ""
    root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
    if not root.startswith("https://"):
        return ""
    if value.startswith("/"):
        return f"{root}{value}"
    return f"{root}/{value}"


def _origins(base_url: str) -> list[str]:
    root = (base_url or get_settings().app_public_url or "").strip().rstrip("/")
    if not root:
        return []
    parsed = urlparse(root if "://" in root else f"https://{root}")
    if not parsed.scheme or not parsed.netloc:
        return [root]
    return [f"{parsed.scheme}://{parsed.netloc}"]


def _normalize_hex(color: str) -> str:
    value = (color or "").strip()
    if len(value) == 7 and value.startswith("#"):
        return value.upper()
    if len(value) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in value):
        return f"#{value.upper()}"
    return "#00A8A8"


def _authorized_session() -> AuthorizedSession:
    settings = get_settings()
    info = _wallet_service_account_info(settings)
    creds = Credentials.from_service_account_info(info, scopes=[WALLET_SCOPE])
    return AuthorizedSession(creds)


def _has_wallet_credentials(settings: Settings) -> bool:
    if settings.google_wallet_service_account_json.strip():
        return True
    path = settings.google_wallet_service_account_file.strip()
    if path and Path(path).is_file():
        return True
    if settings.google_service_account_json.strip():
        return True
    sheets_path = settings.google_service_account_file.strip()
    return bool(sheets_path) and Path(sheets_path).is_file()


def _wallet_service_account_info(settings: Settings) -> dict[str, Any]:
    from bonus.sheets_sync import _parse_service_account_json

    raw = settings.google_wallet_service_account_json.strip()
    if raw:
        return _parse_service_account_json(raw)
    path = settings.google_wallet_service_account_file.strip()
    if path and Path(path).is_file():
        import json

        return json.loads(Path(path).read_text(encoding="utf-8"))
    raw_sheets = settings.google_service_account_json.strip()
    if raw_sheets:
        return _parse_service_account_json(raw_sheets)
    sheets_path = settings.google_service_account_file.strip()
    if sheets_path and Path(sheets_path).is_file():
        import json

        return json.loads(Path(sheets_path).read_text(encoding="utf-8"))
    raise RuntimeError(
        "Нет ключа Google Wallet: задайте GOOGLE_WALLET_SERVICE_ACCOUNT_JSON "
        "или GOOGLE_SERVICE_ACCOUNT_JSON"
    )

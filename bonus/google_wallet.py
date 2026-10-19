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

# cid → fingerprint бренда (лого/имя/цвет). Пересинк при смене шаблона в админке.
_class_fingerprint: dict[str, str] = {}
_class_lock = threading.Lock()


def wallet_enabled(settings: Settings | None = None) -> bool:
    """Включён ли выпуск карт в Google Wallet."""
    cfg = settings or get_settings()
    if not cfg.google_wallet_enabled:
        return False
    if not cfg.google_wallet_issuer_id.strip():
        return False
    return _has_wallet_credentials(cfg)


def class_id(
    settings: Settings | None = None,
    *,
    certificate: Certificate | None = None,
) -> str:
    """Id Loyalty Class: на шаблон админки `{issuer}.tpl_{id}`, иначе общий suffix."""
    cfg = settings or get_settings()
    issuer = cfg.google_wallet_issuer_id.strip()
    template_id = _template_id(certificate)
    if template_id is not None:
        return f"{issuer}.tpl_{template_id}"
    suffix = (cfg.google_wallet_class_suffix or "teams_bonus").strip() or "teams_bonus"
    return f"{issuer}.{suffix}"


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
        # Class создаём/синхроним через REST; в JWT только Object (без reviewStatus).
        try:
            _ensure_loyalty_class(base_url=base_url, certificate=certificate)
        except Exception:
            logger.exception(
                "Google Wallet class sync failed for %s; JWT только с object",
                certificate.public_number or certificate.id,
            )
        payload = build_loyalty_object(
            certificate,
            holder,
            base_url=base_url,
            expired=expired,
        )
        try:
            _upsert_loyalty_object(payload)
        except Exception:
            logger.exception(
                "Google Wallet object upsert failed for %s; выдаём JWT без REST",
                certificate.public_number or certificate.id,
            )
        token = _signed_save_jwt(payload, origins=_origins(base_url))
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
    """Собирает тело Loyalty Object из сертификата и шаблона админки."""
    cfg = settings or get_settings()
    rubles = max(0, int(certificate.balance)) // 100
    short = (certificate.short_code or "").replace("-", "").replace(" ", "")
    name = (holder.full_name if holder is not None else "") or "Сотрудник"
    color = _card_color(certificate, cfg)
    body: dict[str, Any] = {
        "id": object_id(certificate, cfg),
        "classId": class_id(cfg, certificate=certificate),
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
    """Собирает Loyalty Class из шаблона админки (лого, цвет, имя программы)."""
    cfg = settings or get_settings()
    template = _template(certificate)
    logo = _logo_url(base_url, cfg, certificate=certificate)
    issuer = _issuer_name(cfg)
    program = _program_name(cfg, template)
    color = _card_color(certificate, cfg) if certificate is not None else _normalize_hex(
        cfg.google_wallet_class_color or "#00B596"
    )
    if certificate is None and template is not None:
        color = _normalize_hex(getattr(template, "color", None) or color)
    body: dict[str, Any] = {
        "id": class_id(cfg, certificate=certificate),
        "issuerName": issuer,
        "programName": program,
        "accountNameLabel": "Владелец карты",
        "accountIdLabel": "Номер карты",
        "hexBackgroundColor": color,
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
            f"class=per-template (fallback {class_id(settings)})",
            flush=True,
        )
    except Exception as exc:
        print(f"Teams Bonus: Google Wallet проверка не удалась: {exc}", flush=True)


def _ensure_loyalty_class(
    *,
    base_url: str = "",
    certificate: Certificate | None = None,
) -> dict[str, Any]:
    """Создаёт class на шаблон и синхронизирует бренд из админки при отличии."""
    cfg = get_settings()
    payload = build_loyalty_class(base_url=base_url, certificate=certificate, settings=cfg)
    cid = str(payload["id"])
    fingerprint = _branding_fingerprint(payload)
    with _class_lock:
        if _class_fingerprint.get(cid) == fingerprint:
            return payload
        session = _authorized_session()
        existing = session.get(f"{WALLET_API}/loyaltyClass/{cid}")
        if existing.status_code == 200:
            _sync_class_branding(session, cid, payload, existing.json())
            _class_fingerprint[cid] = fingerprint
            return payload
        create_body = {**payload, "reviewStatus": "UNDER_REVIEW"}
        created = session.post(f"{WALLET_API}/loyaltyClass", json=create_body)
        if created.status_code in {200, 409}:
            if created.status_code == 409:
                again = session.get(f"{WALLET_API}/loyaltyClass/{cid}")
                if again.status_code == 200:
                    _sync_class_branding(session, cid, payload, again.json())
            _class_fingerprint[cid] = fingerprint
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
    """Подтягивает лого/имя/цвет из админки. PATCH APPROVED требует UNDER_REVIEW."""
    patch: dict[str, Any] = {}
    for key in (
        "issuerName",
        "programName",
        "accountNameLabel",
        "accountIdLabel",
        "hexBackgroundColor",
        "programLogo",
    ):
        if key not in desired:
            continue
        if desired.get(key) != current.get(key):
            patch[key] = desired[key]
    status = str(current.get("reviewStatus") or "").upper()
    if status == "DRAFT":
        patch["reviewStatus"] = "UNDER_REVIEW"
    elif patch:
        # Временный UNDER_REVIEW на class ≠ Demo Mode эмитента.
        patch["reviewStatus"] = "UNDER_REVIEW"
    if not patch:
        return
    patched = session.patch(f"{WALLET_API}/loyaltyClass/{cid}", json=patch)
    if patched.status_code != 200:
        raise RuntimeError(
            f"loyaltyClass branding failed HTTP {patched.status_code}: {patched.text[:500]}"
        )


def _branding_fingerprint(payload: dict[str, Any]) -> str:
    logo = ""
    program_logo = payload.get("programLogo")
    if isinstance(program_logo, dict):
        logo = str(((program_logo.get("sourceUri") or {}).get("uri")) or "")
    return "|".join(
        [
            str(payload.get("issuerName") or ""),
            str(payload.get("programName") or ""),
            str(payload.get("hexBackgroundColor") or ""),
            logo,
        ]
    )


def _upsert_loyalty_object(payload: dict[str, Any]) -> None:
    """Создаёт Object или обновляет; при смене classId пересоздаёт."""
    oid = str(payload["id"])
    session = _authorized_session()
    existing = session.get(f"{WALLET_API}/loyaltyObject/{oid}")
    if existing.status_code == 200:
        current = existing.json()
        if current.get("classId") != payload.get("classId"):
            deleted = session.delete(f"{WALLET_API}/loyaltyObject/{oid}")
            if deleted.status_code not in {200, 204}:
                logger.warning(
                    "loyaltyObject delete before class switch HTTP %s: %s",
                    deleted.status_code,
                    deleted.text[:300],
                )
            created = session.post(f"{WALLET_API}/loyaltyObject", json=payload)
            if created.status_code not in {200, 409}:
                raise RuntimeError(
                    f"loyaltyObject recreate failed HTTP {created.status_code}: "
                    f"{created.text[:500]}"
                )
            return
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
    origins: list[str],
) -> str:
    """Подписывает JWT Save — только Object; class уже в REST."""
    settings = get_settings()
    info = _wallet_service_account_info(settings)
    email = str(info.get("client_email") or "").strip()
    private_key = str(info.get("private_key") or "")
    claims = {
        "iss": email,
        "aud": "google",
        "typ": "savetowallet",
        "iat": int(time.time()),
        "origins": origins,
        "payload": {"loyaltyObjects": [loyalty_object]},
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
    """Поля как у Statix storeCard: сегмент/скидка/срок."""
    modules: list[dict[str, str]] = []
    segment = _segment_title(certificate)
    if segment:
        modules.append({"id": "segment", "header": "Тип карты", "body": segment})
    if certificate.discount_percent:
        modules.append(
            {
                "id": "discount",
                "header": "Скидка",
                "body": f"{certificate.discount_percent}%",
            }
        )
    if certificate.expires_on is not None:
        modules.append(
            {
                "id": "expires",
                "header": "Действует до",
                "body": certificate.expires_on.strftime("%d.%m.%Y"),
            }
        )
    return modules


def _segment_title(certificate: Certificate) -> str:
    segment = getattr(certificate, "segment", None)
    if segment is not None and getattr(segment, "name", None):
        return str(segment.name).strip()
    return ""


def _hero_image(certificate: Certificate, base_url: str) -> dict[str, Any] | None:
    """Баннер шаблона как heroImage."""
    template = _template(certificate)
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
    """Лого шаблона админки → env → дефолт Mugle."""
    template = _template(certificate)
    logo_path = ""
    if template is not None:
        logo_path = (getattr(template, "logo_path", None) or "").strip()
    if logo_path:
        from bonus.storage import public_media_url

        custom = _absolute_https(public_media_url(logo_path), base_url)
        if custom:
            return custom
    configured = settings.google_wallet_logo_url.strip()
    if configured:
        return configured
    root = (base_url or settings.app_public_url or "").strip().rstrip("/")
    if not root:
        return ""
    return f"{root}/static/wallet-logo.png"


def _template(certificate: Certificate | None) -> Any:
    if certificate is None:
        return None
    return getattr(certificate, "template", None)


def _template_id(certificate: Certificate | None) -> int | None:
    if certificate is None:
        return None
    raw = getattr(certificate, "template_id", None)
    if raw is None:
        template = _template(certificate)
        raw = getattr(template, "id", None) if template is not None else None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _issuer_name(settings: Settings) -> str:
    issuer = (settings.google_wallet_issuer_name or "Mugle Rest").strip()
    if issuer.lower() in {"teams bonus", "teamsbonus"}:
        return "Mugle Rest"
    return issuer or "Mugle Rest"


def _program_name(settings: Settings, template: Any) -> str:
    if template is not None:
        name = str(getattr(template, "name", "") or "").strip()
        if name and name.lower() not in {"teams bonus", "teamsbonus"}:
            return name
    program = (settings.google_wallet_program_name or "Бонус").strip()
    if program.lower() in {"teams bonus", "teamsbonus"}:
        return "Бонус"
    return program or "Бонус"


def _card_color(certificate: Certificate | None, settings: Settings) -> str:
    if certificate is not None:
        color = (getattr(certificate, "color", None) or "").strip()
        if color:
            return _normalize_hex(color)
        template = _template(certificate)
        if template is not None:
            t_color = (getattr(template, "color", None) or "").strip()
            if t_color:
                return _normalize_hex(t_color)
    return _normalize_hex(settings.google_wallet_class_color or "#00B596")


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

"""Сменный провайдер выпуска и обновления pass."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from bonus.apple_wallet import build_pkpass
from bonus.config import Settings, get_settings
from bonus.app_settings import set_wallet_branding_degraded, wallet_branding_degraded
from bonus.audit import write_audit
from bonus.models import AuditAction, Certificate, User
from bonus.wallet_payload import build_walletwallet_body

logger = logging.getLogger("bonus.wallet_provider")

WALLETWALLET_API = "https://api.walletwallet.dev"
USER_ERROR = "Не получилось подготовить карту, попробуйте позже"


@dataclass(frozen=True)
class CreatePassResult:
    """Ответ WalletWallet после создания pass."""

    serial: str
    share_url: str
    google_save_url: str
    pkpass: bytes


class WalletProviderError(Exception):
    """Ошибка провайдера без утечки данных карты."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        quota_exceeded: bool = False,
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.quota_exceeded = quota_exceeded
        self.hint = (hint or "").strip()[:200]


class WalletProvider(Protocol):
    """Интерфейс провайдера pass."""

    def create_pass(
        self,
        db: Session,
        certificate: Certificate,
        holder: User | None,
        *,
        base_url: str,
        show_balance: bool,
        notify_on_change: bool,
    ) -> CreatePassResult:
        """Выпускает pass и возвращает pkpass и метаданные."""

    def update_pass(
        self,
        db: Session,
        certificate: Certificate,
        holder: User | None,
        *,
        serial: str,
        base_url: str,
        show_balance: bool,
        notify_on_change: bool,
    ) -> bool:
        """Обновляет pass; True если тело изменилось."""

    def revoke_pass(self, serial: str) -> None:
        """Отзывает pass по serial."""


def get_wallet_provider(settings: Settings | None = None) -> WalletProvider:
    """Выбирает провайдер по WALLET_PROVIDER."""
    cfg = settings or get_settings()
    mode = (cfg.wallet_provider or "walletwallet").strip().casefold()
    if mode == "local":
        return LocalPkpassProvider(cfg)
    return WalletWalletProvider(cfg)


def wallet_provider_ready(settings: Settings | None = None) -> bool:
    """Можно ли вызывать WalletWallet / локальную подпись."""
    cfg = settings or get_settings()
    mode = (cfg.wallet_provider or "walletwallet").strip().casefold()
    if mode == "local":
        from bonus.apple_wallet import apple_wallet_enabled

        return apple_wallet_enabled(cfg)
    return bool(_normalize_api_key(cfg.walletwallet_api_key))


def wallet_provider_diagnostics(db: Session | None = None) -> dict[str, bool | str]:
    """Для админки: без значения ключа."""
    settings = get_settings()
    key = _normalize_api_key(settings.walletwallet_api_key)
    return {
        "provider": (settings.wallet_provider or "walletwallet").strip() or "walletwallet",
        "key_configured": bool(key),
        "key_prefix_ok": key.startswith("ww_live_"),
        "branding_pro_degraded": wallet_branding_degraded(db) if db is not None else False,
    }


def log_wallet_provider_status() -> None:
    """Сообщение при старте приложения."""
    settings = get_settings()
    mode = (settings.wallet_provider or "walletwallet").strip().casefold()
    if mode == "local":
        from bonus.apple_wallet import apple_wallet_enabled

        if apple_wallet_enabled(settings):
            print("Teams Bonus: Wallet local pkpass OK", flush=True)
        else:
            print("Teams Bonus: Wallet local — подпись Apple не настроена", flush=True)
        return
    if not wallet_provider_ready(settings):
        print("Teams Bonus: WalletWallet выключен — нет WALLETWALLET_API_KEY", flush=True)
        return
    print("Teams Bonus: WalletWallet провайдер готов", flush=True)


class WalletWalletProvider:
    """WalletWallet API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_pass(
        self,
        db: Session,
        certificate: Certificate,
        holder: User | None,
        *,
        base_url: str,
        show_balance: bool,
        notify_on_change: bool,
    ) -> CreatePassResult:
        data = _post_passes(
            db,
            certificate,
            holder,
            base_url=base_url,
            show_balance=show_balance,
            notify_on_change=notify_on_change,
        )
        serial = str(data.get("serialNumber") or "").strip()
        share = str(data.get("shareUrl") or "").strip()
        google = str(data.get("googleSaveUrl") or "").strip()
        raw_b64 = _apple_pass_b64(data.get("applePass"))
        if not serial:
            raise WalletProviderError(USER_ERROR)
        if not raw_b64:
            if share:
                return CreatePassResult(
                    serial=serial,
                    share_url=share,
                    google_save_url=google,
                    pkpass=b"",
                )
            raise WalletProviderError(USER_ERROR)
        try:
            pkpass = _decode_pkpass_bytes(raw_b64)
        except Exception as exc:
            logger.warning("WalletWallet applePass decode failed: %s", type(exc).__name__)
            if share:
                return CreatePassResult(
                    serial=serial,
                    share_url=share,
                    google_save_url=google,
                    pkpass=b"",
                )
            raise WalletProviderError(USER_ERROR) from exc
        return CreatePassResult(
            serial=serial,
            share_url=share,
            google_save_url=google,
            pkpass=pkpass,
        )

    def update_pass(
        self,
        db: Session,
        certificate: Certificate,
        holder: User | None,
        *,
        serial: str,
        base_url: str,
        show_balance: bool,
        notify_on_change: bool,
    ) -> bool:
        path = f"/api/passes/{serial}"
        data = _put_pass(
            db,
            certificate,
            holder,
            path=path,
            base_url=base_url,
            show_balance=show_balance,
            notify_on_change=notify_on_change,
        )
        return not bool(data.get("unchanged"))

    def revoke_pass(self, serial: str) -> None:
        cleaned = (serial or "").strip()
        if not cleaned:
            return
        _delete(f"/api/passes/{cleaned}")


class LocalPkpassProvider:
    """Заготовка под локальную подпись Apple."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_pass(
        self,
        db: Session,
        certificate: Certificate,
        holder: User | None,
        *,
        base_url: str,
        show_balance: bool,
        notify_on_change: bool,
    ) -> CreatePassResult:
        del db, show_balance, notify_on_change
        payload = build_pkpass(certificate, holder, base_url=base_url)
        if not payload:
            raise WalletProviderError(USER_ERROR)
        serial = certificate.public_number or str(certificate.id)
        return CreatePassResult(
            serial=serial,
            share_url="",
            google_save_url="",
            pkpass=payload,
        )

    def update_pass(
        self,
        db: Session,
        certificate: Certificate,
        holder: User | None,
        *,
        serial: str,
        base_url: str,
        show_balance: bool,
        notify_on_change: bool,
    ) -> bool:
        del db, certificate, holder, serial, base_url, show_balance, notify_on_change
        raise NotImplementedError("Локальное обновление pass пока не реализовано")

    def revoke_pass(self, serial: str) -> None:
        del serial


def _apple_pass_b64(raw: object) -> str:
    """Тело applePass из JSON: только строка base64."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("ascii", errors="ignore").strip()
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _decode_pkpass_bytes(raw_b64: str) -> bytes:
    """Декодирует applePass: обычный и urlsafe base64."""
    cleaned = "".join(raw_b64.split())
    try:
        return base64.b64decode(cleaned, validate=True)
    except Exception:
        padded = cleaned + "=" * (-len(cleaned) % 4)
        return base64.urlsafe_b64decode(padded)


def _post_passes(
    db: Session,
    certificate: Certificate,
    holder: User | None,
    *,
    base_url: str,
    show_balance: bool,
    notify_on_change: bool,
) -> dict[str, Any]:
    """POST /api/passes с откатом Pro-полей при 403."""
    kwargs = dict(
        db=db,
        certificate=certificate,
        holder=holder,
        base_url=base_url,
        show_balance=show_balance,
        notify_on_change=notify_on_change,
    )
    try:
        data = _post_json("/api/passes", build_walletwallet_body(**kwargs, branding_pro=True))
        _clear_branding_degraded(db)
        return data
    except WalletProviderError as exc:
        if exc.status_code != 403:
            raise
        logger.warning("WalletWallet POST /api/passes Pro → 403, fallback без logoURL/stripURL/color")
        _record_branding_fallback(db)
        return _post_json("/api/passes", build_walletwallet_body(**kwargs, branding_pro=False))


def _put_pass(
    db: Session,
    certificate: Certificate,
    holder: User | None,
    *,
    path: str,
    base_url: str,
    show_balance: bool,
    notify_on_change: bool,
) -> dict[str, Any]:
    """PUT /api/passes/<serial> с откатом Pro-полей при 403."""
    kwargs = dict(
        db=db,
        certificate=certificate,
        holder=holder,
        base_url=base_url,
        show_balance=show_balance,
        notify_on_change=notify_on_change,
    )
    try:
        data = _put_json(path, build_walletwallet_body(**kwargs, branding_pro=True))
        _clear_branding_degraded(db)
        return data
    except WalletProviderError as exc:
        if exc.status_code != 403:
            raise
        logger.warning("WalletWallet PUT %s Pro → 403, fallback без logoURL/stripURL/color", path)
        _record_branding_fallback(db)
        return _put_json(path, build_walletwallet_body(**kwargs, branding_pro=False))


def _record_branding_fallback(db: Session) -> None:
    """Помечает пилот как работающий без тарифа Pro."""
    set_wallet_branding_degraded(db, True)
    write_audit(
        db,
        actor=None,
        action=AuditAction.wallet_settings,
        summary="Wallet: брендинг Pro недоступен",
        target_type="app_settings",
        target_id=1,
        detail="Wallet работает без брендинга: нужен тариф Pro",
    )


def _clear_branding_degraded(db: Session) -> None:
    """Сбрасывает предупреждение после успешного Pro-запроса."""
    if wallet_branding_degraded(db):
        set_wallet_branding_degraded(db, False)


def _normalize_api_key(raw: str) -> str:
    """Ключ без Bearer; дописывает ww_live_ если передали только 32 hex."""
    key = (raw or "").strip()
    if not key:
        return ""
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if not key.startswith("ww_live_"):
        compact = key.replace("-", "")
        if len(compact) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in compact):
            key = f"ww_live_{compact.lower()}"
    return key


def _auth_header(settings: Settings) -> str:
    key = _normalize_api_key(settings.walletwallet_api_key)
    if not key:
        raise WalletProviderError(USER_ERROR, status_code=401)
    return f"Bearer {key}"


def _log_api_error(method: str, path: str, status: int, hint: str = "") -> None:
    if hint:
        logger.warning("WalletWallet %s %s → HTTP %s (%s)", method, path, status, hint)
    else:
        logger.warning("WalletWallet %s %s → HTTP %s", method, path, status)


def _handle_response(response: httpx.Response, method: str, path: str) -> dict[str, Any]:
    status = response.status_code
    if status < 400:
        if not response.content:
            return {}
        try:
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    hint = _response_error_hint(response)
    _log_api_error(method, path, status, hint)
    quota = status == 429
    if status in {400, 401, 403, 404, 429, 500}:
        raise WalletProviderError(
            USER_ERROR,
            status_code=status,
            quota_exceeded=quota,
            hint=hint,
        )
    raise WalletProviderError(
        USER_ERROR,
        status_code=status,
        quota_exceeded=quota,
        hint=hint,
    )


def _response_error_hint(response: httpx.Response) -> str:
    """Краткая подсказка из JSON ошибки без тела pass."""
    try:
        data = response.json()
        if isinstance(data, dict):
            for key in ("error", "message", "detail"):
                text = str(data.get(key) or "").strip()
                if text:
                    return text[:160]
    except Exception:
        pass
    return ""


def _post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    headers = {
        "Authorization": _auth_header(settings),
        "Content-Type": "application/json",
    }
    url = f"{WALLETWALLET_API}{path}"
    try:
        with httpx.Client(timeout=45.0) as client:
            response = client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning("WalletWallet POST %s transport error: %s", path, type(exc).__name__)
        raise WalletProviderError(USER_ERROR) from exc
    return _handle_response(response, "POST", path)


def _put_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    headers = {
        "Authorization": _auth_header(settings),
        "Content-Type": "application/json",
    }
    url = f"{WALLETWALLET_API}{path}"
    try:
        with httpx.Client(timeout=45.0) as client:
            response = client.put(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        logger.warning("WalletWallet PUT %s transport error: %s", path, type(exc).__name__)
        raise WalletProviderError(USER_ERROR) from exc
    return _handle_response(response, "PUT", path)


def _delete(path: str) -> None:
    settings = get_settings()
    headers = {"Authorization": _auth_header(settings)}
    url = f"{WALLETWALLET_API}{path}"
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.delete(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("WalletWallet DELETE %s transport error: %s", path, type(exc).__name__)
        return
    if response.status_code >= 400:
        _log_api_error("DELETE", path, response.status_code)

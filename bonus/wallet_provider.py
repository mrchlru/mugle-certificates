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
from bonus.models import Certificate, User
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

    def __init__(self, message: str, *, status_code: int = 0, quota_exceeded: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.quota_exceeded = quota_exceeded


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
    key = (settings.walletwallet_api_key or "").strip()
    if not key:
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
        body = build_walletwallet_body(
            db,
            certificate,
            holder,
            base_url=base_url,
            show_balance=show_balance,
            notify_on_change=notify_on_change,
        )
        data = _post_json("/api/passes", body)
        serial = str(data.get("serialNumber") or "").strip()
        share = str(data.get("shareUrl") or "").strip()
        google = str(data.get("googleSaveUrl") or "").strip()
        raw_b64 = str(data.get("applePass") or "").strip()
        if not serial or not raw_b64:
            raise WalletProviderError(USER_ERROR)
        try:
            pkpass = base64.b64decode(raw_b64, validate=True)
        except Exception as exc:
            logger.warning("WalletWallet applePass decode failed: %s", type(exc).__name__)
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
        body = build_walletwallet_body(
            db,
            certificate,
            holder,
            base_url=base_url,
            show_balance=show_balance,
            notify_on_change=notify_on_change,
        )
        path = f"/api/passes/{serial}"
        data = _put_json(path, body)
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


def _auth_header(settings: Settings) -> str:
    key = (settings.walletwallet_api_key or "").strip()
    if not key:
        raise WalletProviderError(USER_ERROR, status_code=401)
    if key.lower().startswith("bearer "):
        return key
    return f"Bearer {key}"


def _log_api_error(method: str, path: str, status: int) -> None:
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
    _log_api_error(method, path, status)
    quota = status == 429
    if status in {400, 401, 403, 404, 429, 500}:
        raise WalletProviderError(USER_ERROR, status_code=status, quota_exceeded=quota)
    raise WalletProviderError(USER_ERROR, status_code=status, quota_exceeded=quota)


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

"""Выдача pass, rate limit и отзыв."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bonus.audit import write_audit
from bonus.config import get_settings
from bonus.models import AuditAction, Certificate, CertificateStatus, WalletFetchLog, WalletPass
from bonus.wallet_provider import WalletProviderError, get_wallet_provider

from bonus import app_settings as app_settings_module
from bonus.values import today_msk

logger = logging.getLogger("bonus.wallet_service")

USER_ERROR = "Не получилось подготовить карту, попробуйте позже"
PKPASS_NAME = "teams-bonus.pkpass"


def certificate_wallet_visible(db: Session, certificate: Certificate) -> bool:
    """Кнопка Apple Wallet на странице карты."""
    if not app_settings_module.wallet_feature_enabled(db):
        return False
    if not bool(getattr(certificate, "wallet_allowed", False)):
        return False
    if _unavailable(certificate):
        return False
    return True


def wallet_ui_context(db: Session, certificate: Certificate) -> dict[str, str | bool]:
    """Контекст шаблона public_card: только новый WalletWallet-пилот."""
    if not certificate_wallet_visible(db, certificate):
        return {
            "apple_wallet_enabled": False,
            "apple_wallet_href": "",
            "wallet_show_balance_on_pass": False,
            "wallet_hint": "",
            "walletpasses_enabled": False,
            "walletpasses_href": "",
        }
    show_bal = app_settings_module.wallet_show_balance(db)
    hint = ""
    if not show_bal:
        hint = "Карта сохранится в Wallet. Баланс смотрите в приложении."
    return {
        "apple_wallet_enabled": True,
        "apple_wallet_href": f"/c/{certificate.token}/wallet",
        "wallet_show_balance_on_pass": show_bal,
        "wallet_hint": hint,
        "walletpasses_enabled": False,
        "walletpasses_href": "",
    }


def serve_wallet_pass(
    db: Session,
    certificate: Certificate,
    *,
    base_url: str,
    client_key: str,
    actor_id: int | None = None,
) -> Response | RedirectResponse:
    """GET /c/{token}/wallet — pkpass, редирект или ошибка."""
    if not certificate_wallet_visible(db, certificate):
        return Response(status_code=404)
    if not _rate_limit_ok(db, certificate.id, client_key):
        return Response(
            content="Слишком много запросов, попробуйте завтра.",
            status_code=429,
            media_type="text/plain; charset=utf-8",
        )
    row = db.scalar(select(WalletPass).where(WalletPass.certificate_id == certificate.id))
    if row is not None and row.revoked_at is not None:
        row = None
    if row is not None:
        share = (row.share_url or "").strip()
        if share:
            _audit_issue(db, certificate, "redirect", actor_id)
            return RedirectResponse(share, status_code=302)
        return Response(
            content=USER_ERROR,
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    show_bal = app_settings_module.wallet_show_balance(db)
    notify = app_settings_module.wallet_notify_on_change(db)
    holder = certificate.holder
    provider = get_wallet_provider()
    try:
        created = provider.create_pass(
            db,
            certificate,
            holder,
            base_url=base_url,
            show_balance=show_bal,
            notify_on_change=notify,
        )
        now = _utcnow()
        db.add(
            WalletPass(
                certificate_id=certificate.id,
                serial=created.serial,
                share_url=created.share_url,
                google_save_url=created.google_save_url,
                created_at=now,
                last_pushed_at=now,
                sync_error="",
            )
        )
        db.commit()
        _audit_issue(db, certificate, "created", actor_id)
        return Response(
            content=created.pkpass,
            media_type="application/vnd.apple.pkpass",
            headers={
                "Content-Disposition": f'attachment; filename="{PKPASS_NAME}"',
            },
        )
    except WalletProviderError as exc:
        if exc.quota_exceeded:
            app_settings_module.pause_wallet_provider(db, hours=1)
            write_audit(
                db,
                actor=None,
                action=AuditAction.wallet_provider_quota,
                summary="WalletWallet: исчерпана квота",
                target_type="app_settings",
                target_id=1,
                detail="Кнопка Wallet приостановлена на 1 час",
            )
        _audit_issue(db, certificate, "error", actor_id)
        if exc.status_code == 429:
            return Response(
                content=USER_ERROR,
                status_code=429,
                media_type="text/plain; charset=utf-8",
            )
        return Response(
            content=USER_ERROR,
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    return Response(status_code=404)


def revoke_wallet_pass(db: Session, certificate: Certificate) -> None:
    """DELETE у провайдера и revoked_at в БД."""
    row = db.scalar(select(WalletPass).where(WalletPass.certificate_id == certificate.id))
    if row is None or row.revoked_at is not None:
        return
    serial = (row.serial or "").strip()
    if serial:
        try:
            get_wallet_provider().revoke_pass(serial)
        except Exception:
            logger.warning("Wallet revoke failed for cert %s", certificate.id)
    row.revoked_at = _utcnow()
    row.sync_error = ""
    db.commit()


def _unavailable(certificate: Certificate) -> bool:
    if certificate.is_deleted:
        return True
    if certificate.status == CertificateStatus.blocked:
        return True
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return True
    return False


def _rate_limit_ok(db: Session, certificate_id: int, client_key: str) -> bool:
    settings = get_settings()
    since = _utcnow() - timedelta(days=1)
    per_card = settings.wallet_fetch_per_card_per_day
    per_ip = settings.wallet_fetch_per_ip_per_day
    card_count = db.scalar(
        select(func.count())
        .select_from(WalletFetchLog)
        .where(
            WalletFetchLog.certificate_id == certificate_id,
            WalletFetchLog.created_at >= since,
        )
    )
    if (card_count or 0) >= per_card:
        return False
    ip_key = _hash_client(client_key)
    ip_count = db.scalar(
        select(func.count())
        .select_from(WalletFetchLog)
        .where(
            WalletFetchLog.client_key == ip_key,
            WalletFetchLog.created_at >= since,
        )
    )
    if (ip_count or 0) >= per_ip:
        return False
    db.add(
        WalletFetchLog(
            certificate_id=certificate_id,
            client_key=ip_key,
            created_at=_utcnow(),
        )
    )
    db.commit()
    return True


def _hash_client(client_key: str) -> str:
    cleaned = (client_key or "local").strip() or "local"
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:32]


def _audit_issue(
    db: Session,
    certificate: Certificate,
    result: str,
    actor_id: int | None,
) -> None:
    from bonus.models import User

    actor = db.get(User, actor_id) if actor_id else None
    write_audit(
        db,
        actor=actor,
        action=AuditAction.wallet_issue,
        summary=f"Wallet · {certificate.public_number}",
        target_type="certificate",
        target_id=certificate.id,
        detail=result,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

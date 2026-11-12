"""Выдача pass, rate limit и отзыв."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from bonus.audit import write_audit
from bonus.config import get_settings
from bonus.models import AuditAction, Certificate, CertificateStatus, WalletFetchLog, WalletPass
from bonus.wallet_provider import (
    CreatePassResult,
    WalletProviderError,
    get_wallet_provider,
    wallet_provider_ready,
)

from bonus import app_settings as app_settings_module
from bonus.values import today_msk

logger = logging.getLogger("bonus.wallet_service")

USER_ERROR = "Не получилось подготовить карту, попробуйте позже"
PKPASS_NAME = "teams-bonus.pkpass"


def certificate_wallet_visible(db: Session, certificate: Certificate) -> bool:
    """Кнопка Apple Wallet на странице карты."""
    if not wallet_provider_ready():
        return False
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
    if not _rate_limit_allows(db, certificate.id, client_key):
        logger.warning(
            "Wallet rate limit cert=%s client=%s",
            certificate.public_number or certificate.id,
            _hash_client(client_key)[:8],
        )
        return Response(
            content="Слишком много запросов, попробуйте завтра.",
            status_code=429,
            media_type="text/plain; charset=utf-8",
        )
    row = db.scalar(select(WalletPass).where(WalletPass.certificate_id == certificate.id))
    if row is not None and row.revoked_at is not None:
        row = None
    if row is not None and not (row.share_url or "").strip():
        db.delete(row)
        db.commit()
        row = None
    if row is not None:
        share = (row.share_url or "").strip()
        if share:
            _audit_issue(db, certificate, "redirect", actor_id)
            _rate_limit_record(db, certificate.id, client_key)
            return RedirectResponse(share, status_code=302)
        return Response(
            content=USER_ERROR,
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )
    if not wallet_provider_ready():
        logger.warning(
            "Wallet issue skipped: WALLETWALLET_API_KEY not set (cert %s)",
            certificate.public_number or certificate.id,
        )
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
        share = (created.share_url or "").strip()
        if not created.pkpass and share:
            try:
                _store_wallet_pass_row(db, certificate.id, created)
                db.commit()
            except SQLAlchemyError:
                db.rollback()
            _audit_issue(db, certificate, "redirect", actor_id)
            _rate_limit_record(db, certificate.id, client_key)
            return RedirectResponse(share, status_code=302)
        now = _utcnow()
        try:
            _store_wallet_pass_row(db, certificate.id, created, now=now)
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.scalar(
                select(WalletPass).where(WalletPass.certificate_id == certificate.id)
            )
            existing_share = (row.share_url if row else "") or ""
            if existing_share.strip():
                _audit_issue(db, certificate, "redirect", actor_id)
                _rate_limit_record(db, certificate.id, client_key)
                return RedirectResponse(existing_share.strip(), status_code=302)
            logger.warning(
                "Wallet pass row conflict cert=%s",
                certificate.public_number or certificate.id,
            )
            return Response(
                content=USER_ERROR,
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )
        except SQLAlchemyError as exc:
            db.rollback()
            logger.exception(
                "Wallet pass DB save failed cert=%s: %s",
                certificate.public_number or certificate.id,
                type(exc).__name__,
            )
            if created.pkpass:
                return Response(
                    content=created.pkpass,
                    media_type="application/vnd.apple.pkpass",
                    headers={
                        "Content-Disposition": f'attachment; filename="{PKPASS_NAME}"',
                    },
                )
            if share:
                return RedirectResponse(share, status_code=302)
            return Response(
                content=USER_ERROR,
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )
        _audit_issue(db, certificate, "created", actor_id)
        _rate_limit_record(db, certificate.id, client_key)
        logger.info(
            "Wallet pass created cert=%s serial_len=%s pkpass_bytes=%s",
            certificate.public_number or certificate.id,
            len(created.serial or ""),
            len(created.pkpass),
        )
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
        logger.warning(
            "Wallet issue failed cert=%s http=%s quota=%s",
            certificate.public_number or certificate.id,
            exc.status_code,
            exc.quota_exceeded,
        )
        _audit_issue(
            db,
            certificate,
            _wallet_issue_error_detail(exc),
            actor_id,
        )
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


def _rate_limit_allows(db: Session, certificate_id: int, client_key: str) -> bool:
    """Проверка лимитов без записи (неудачные попытки не съедают квоту)."""
    settings = get_settings()
    since = _utcnow() - timedelta(days=1)
    per_card = settings.wallet_fetch_per_card_per_day
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
    if _use_ip_rate_limit(client_key):
        per_ip = settings.wallet_fetch_per_ip_per_day
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
    return True


def _rate_limit_record(db: Session, certificate_id: int, client_key: str) -> None:
    """Фиксирует успешную выдачу для лимитов."""
    db.add(
        WalletFetchLog(
            certificate_id=certificate_id,
            client_key=_hash_client(client_key),
            created_at=_utcnow(),
        )
    )
    db.commit()


def _use_ip_rate_limit(client_key: str) -> bool:
    """За Docker-прокси все запросы с 172.x — не душим весь сайт одним лимитом."""
    raw = (client_key or "").strip()
    if not raw or raw == "local":
        return False
    if raw.startswith("172.") or raw.startswith("10.") or raw.startswith("192.168."):
        return False
    return True


def _store_wallet_pass_row(
    db: Session,
    certificate_id: int,
    created: CreatePassResult,
    *,
    now: datetime | None = None,
) -> None:
    """Сохраняет pass в wallet_passes с обрезкой под VARCHAR."""
    stamp = now or _utcnow()
    db.add(
        WalletPass(
            certificate_id=certificate_id,
            serial=(created.serial or "")[:80],
            share_url=(created.share_url or "")[:500],
            google_save_url=(created.google_save_url or "")[:500],
            created_at=stamp,
            last_pushed_at=stamp,
            sync_error="",
        )
    )


def _hash_client(client_key: str) -> str:
    cleaned = (client_key or "local").strip() or "local"
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:32]


def _wallet_issue_error_detail(exc: WalletProviderError) -> str:
    """Краткая причина для журнала (без тела pass и без ключа)."""
    parts = ["error"]
    if exc.status_code:
        parts.append(f"http={exc.status_code}")
    if exc.hint:
        parts.append(exc.hint)
    return " ".join(parts)[:200]


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

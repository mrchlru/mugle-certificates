"""Фоновое обновление pass в WalletWallet после смены баланса."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus import app_settings as app_settings_module
from bonus.db import open_session
from bonus.models import Certificate, WalletPass, WalletSyncJob
from bonus.wallet_provider import WalletProviderError, get_wallet_provider

logger = logging.getLogger("bonus.wallet_queue")

_MAX_ATTEMPTS = 8
_POLL_IDLE_SEC = 4.0
_SLEEP_ON_ERROR_SEC = 25.0


def enqueue_wallet_update(db: Session, certificate_id: int) -> None:
    """Ставит карту в очередь PUT, если включён баланс на pass и доступ выдан."""
    if not certificate_id:
        return
    if not app_settings_module.wallet_show_balance(db):
        return
    cert = db.get(Certificate, certificate_id)
    if cert is None or not app_settings_module.certificate_wallet_allowed(cert):
        return
    job = db.scalar(select(WalletSyncJob).where(WalletSyncJob.certificate_id == certificate_id))
    now = _utcnow()
    if job is None:
        db.add(
            WalletSyncJob(
                certificate_id=certificate_id,
                status="pending",
                attempts=0,
                last_error="",
                created_at=now,
                updated_at=now,
            )
        )
    else:
        job.status = "pending"
        job.attempts = 0
        job.last_error = ""
        job.updated_at = now
    db.commit()


async def wallet_queue_worker() -> None:
    """Воркер PUT /api/passes."""
    while True:
        try:
            worked = await asyncio.to_thread(_run_one_job)
            if not worked:
                await asyncio.sleep(_POLL_IDLE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("wallet queue worker error")
            await asyncio.sleep(_POLL_IDLE_SEC)


def _run_one_job() -> bool:
    db = open_session()
    try:
        if not app_settings_module.wallet_show_balance(db):
            return False
        job = db.scalar(
            select(WalletSyncJob)
            .where(WalletSyncJob.status == "pending")
            .order_by(WalletSyncJob.updated_at.asc())
            .limit(1)
        )
        if job is None:
            return False
        certificate = db.get(Certificate, job.certificate_id)
        if certificate is None:
            job.status = "done"
            job.updated_at = _utcnow()
            db.commit()
            return True
        if not app_settings_module.certificate_wallet_allowed(certificate):
            job.status = "done"
            job.updated_at = _utcnow()
            db.commit()
            return True
        pass_row = db.scalar(
            select(WalletPass).where(
                WalletPass.certificate_id == certificate.id,
                WalletPass.revoked_at.is_(None),
            )
        )
        if pass_row is None or not (pass_row.serial or "").strip():
            job.status = "done"
            job.updated_at = _utcnow()
            db.commit()
            return True
        job.status = "running"
        job.updated_at = _utcnow()
        db.commit()
        from bonus.config import get_settings

        base_url = (get_settings().app_public_url or "").strip()
        show_bal = app_settings_module.wallet_show_balance(db)
        notify = app_settings_module.wallet_notify_on_change(db)
        provider = get_wallet_provider()
        try:
            changed = provider.update_pass(
                db,
                certificate,
                certificate.holder,
                serial=pass_row.serial,
                base_url=base_url,
                show_balance=show_bal,
                notify_on_change=notify,
            )
            pass_row.last_pushed_at = _utcnow()
            pass_row.sync_error = ""
            job.status = "done"
            job.updated_at = _utcnow()
            if not changed:
                job.last_error = ""
            db.commit()
            return True
        except WalletProviderError as exc:
            job.attempts += 1
            job.last_error = str(exc)[:400]
            job.status = "pending" if job.attempts < _MAX_ATTEMPTS else "error"
            job.updated_at = _utcnow()
            pass_row.sync_error = "Wallet не синхронизирован"
            db.commit()
            if job.status == "error":
                logger.warning(
                    "Wallet sync abandoned cert_id=%s attempts=%s",
                    certificate.id,
                    job.attempts,
                )
            return True
    finally:
        db.close()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

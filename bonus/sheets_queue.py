"""Фоновая очередь записи сертификатов в Google Sheets.

Пишет небольшими пачками с паузами и ретраями, чтобы не ловить квоту API.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bonus.db import open_session
from bonus.models import Certificate, CertificateStatus, SheetsSyncJob
from bonus.sheets_sync import (
    SheetIssuedItem,
    sheets_credentials_error,
    sync_certificates_issued_batch,
)

logger = logging.getLogger("bonus.sheets_queue")

_BATCH_SIZE = 40
_SLEEP_BETWEEN_BATCHES_SEC = 2.5
_MAX_ATTEMPTS = 12
_POLL_IDLE_SEC = 5.0


def enqueue_certificate_ids(db: Session, certificate_ids: list[int]) -> int:
    """Ставит карты в очередь (или возвращает в pending). Возвращает число затронутых."""
    unique_ids = sorted({int(item) for item in certificate_ids if item})
    if not unique_ids:
        return 0
    existing = {
        job.certificate_id: job
        for job in db.scalars(
            select(SheetsSyncJob).where(SheetsSyncJob.certificate_id.in_(unique_ids))
        ).all()
    }
    touched = 0
    now = _utcnow()
    for certificate_id in unique_ids:
        job = existing.get(certificate_id)
        if job is None:
            db.add(
                SheetsSyncJob(
                    certificate_id=certificate_id,
                    status="pending",
                    attempts=0,
                    last_error="",
                    created_at=now,
                    updated_at=now,
                )
            )
            touched += 1
            continue
        if job.status == "pending":
            continue
        job.status = "pending"
        job.attempts = 0
        job.last_error = ""
        job.updated_at = now
        touched += 1
    db.commit()
    return touched


def queue_stats(db: Session) -> dict[str, int]:
    """Счётчики очереди для UI."""
    rows = db.execute(
        select(SheetsSyncJob.status, func.count()).group_by(SheetsSyncJob.status)
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    return {
        "pending": counts.get("pending", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
    }


def certificate_sync_status(db: Session, certificate_id: int) -> str:
    """Статус синка карты: pending | done | failed | none."""
    job = db.scalar(
        select(SheetsSyncJob).where(SheetsSyncJob.certificate_id == certificate_id)
    )
    if job is None:
        return "none"
    status = (job.status or "").strip().lower()
    if status in {"pending", "done", "failed"}:
        return status
    return "pending"


def mark_certificate_job(
    db: Session,
    certificate_id: int,
    status: str,
    last_error: str = "",
) -> None:
    """Обновляет статус задачи очереди для одной карты."""
    job = db.scalar(
        select(SheetsSyncJob).where(SheetsSyncJob.certificate_id == certificate_id)
    )
    now = _utcnow()
    if job is None:
        db.add(
            SheetsSyncJob(
                certificate_id=certificate_id,
                status=status,
                attempts=1 if status != "pending" else 0,
                last_error=(last_error or "")[:500],
                created_at=now,
                updated_at=now,
            )
        )
    else:
        job.status = status
        job.last_error = (last_error or "")[:500]
        job.updated_at = now
        if status == "done":
            job.attempts = max(job.attempts, 1)
        elif status == "failed":
            job.attempts += 1
    db.commit()


def process_sheets_queue_once(limit: int = _BATCH_SIZE) -> dict[str, int]:
    """Обрабатывает одну пачку pending-задач. Для воркера и тестов."""
    cred_error = sheets_credentials_error()
    if cred_error:
        logger.error("Google Sheets очередь стоит: %s", cred_error)
        return {"written": 0, "skipped": 0, "errors": 0, "blocked": 1}

    db = open_session()
    try:
        jobs = list(
            db.scalars(
                select(SheetsSyncJob)
                .where(SheetsSyncJob.status == "pending")
                .order_by(SheetsSyncJob.id)
                .limit(limit)
            ).all()
        )
        if not jobs:
            return {"written": 0, "skipped": 0, "errors": 0, "blocked": 0}

        certificate_ids = [job.certificate_id for job in jobs]
        certificates = {
            item.id: item
            for item in db.scalars(
                select(Certificate).where(
                    Certificate.id.in_(certificate_ids),
                    Certificate.status == CertificateStatus.active,
                )
            ).all()
        }
        items: list[SheetIssuedItem] = []
        job_by_cert = {job.certificate_id: job for job in jobs}
        now = _utcnow()
        for certificate_id in certificate_ids:
            job = job_by_cert[certificate_id]
            certificate = certificates.get(certificate_id)
            if certificate is None or certificate.holder is None:
                job.status = "failed"
                job.attempts += 1
                job.last_error = "Сертификат не найден или неактивен"
                job.updated_at = now
                continue
            segment_name = ""
            if certificate.segment is not None:
                segment_name = (certificate.segment.name or "").strip()
            items.append(
                SheetIssuedItem(
                    certificate=certificate,
                    holder=certificate.holder,
                    segment_name=segment_name,
                )
            )

        result = sync_certificates_issued_batch(items) if items else None
        ok_set = set(result.ok_ids) if result else set()
        fail_set = set(result.failed_ids) if result else set()
        for item in items:
            job = job_by_cert[item.certificate.id]
            job.attempts += 1
            job.updated_at = now
            if item.certificate.id in ok_set and item.certificate.id not in fail_set:
                job.status = "done"
                job.last_error = ""
                continue
            job.last_error = "Пакет Sheets не записан (см. лог)"
            if job.attempts >= _MAX_ATTEMPTS:
                job.status = "failed"
            else:
                job.status = "pending"
        db.commit()
        written = 0 if result is None else result.written
        skipped = 0 if result is None else result.skipped
        errors = 0 if result is None else result.errors
        if written or skipped or errors:
            print(
                f"Teams Bonus: Sheets очередь — +{written}, уже были {skipped}, "
                f"ошибок пакета {errors}",
                flush=True,
            )
        return {
            "written": written,
            "skipped": skipped,
            "errors": errors,
            "blocked": 0,
        }
    finally:
        db.close()


async def sheets_queue_worker() -> None:
    """Фоновый цикл: пачками дописывает очередь в Google Sheets."""
    while True:
        try:
            stats = await asyncio.to_thread(process_sheets_queue_once)
            if stats.get("blocked"):
                await asyncio.sleep(30.0)
                continue
            if stats["written"] or stats["skipped"] or stats["errors"]:
                await asyncio.sleep(_SLEEP_BETWEEN_BATCHES_SEC)
            else:
                await asyncio.sleep(_POLL_IDLE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой очереди Google Sheets")
            await asyncio.sleep(10.0)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

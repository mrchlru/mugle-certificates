"""Фоновая очередь записи сертификатов в Google Sheets.

Все операции с таблицей (синк карты, расходы, удаление строки) идут через
один воркер с паузами, без параллельных потоков — чтобы не упираться в квоту API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bonus.db import open_session
from bonus.models import Certificate, SheetsSyncJob, User
from bonus.sheets_sync import (
    delete_certificate_row,
    list_sheet_usages,
    sheets_credentials_error,
    sheets_enabled,
    sync_certificate_usage,
)
from bonus.values import today_msk

logger = logging.getLogger("bonus.sheets_queue")

_BATCH_SIZE = 4
_SLEEP_BETWEEN_JOBS_SEC = 2.0
_QUOTA_BACKOFF_SEC = 55.0
_MAX_ATTEMPTS = 12
_POLL_IDLE_SEC = 5.0
_SLEEP_AFTER_WORK_SEC = 2.5


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
                    job_kind="sync",
                    status="pending",
                    attempts=0,
                    last_error="",
                    created_at=now,
                    updated_at=now,
                )
            )
            touched += 1
            continue
        if job.status == "pending" and job.job_kind == "sync":
            job.updated_at = now
            continue
        job.status = "pending"
        job.job_kind = "sync"
        job.sheet_public_number = ""
        job.sheet_ledger_sheet = ""
        job.attempts = 0
        job.last_error = ""
        job.updated_at = now
        touched += 1
    db.commit()
    return touched


def enqueue_certificate_delete(db: Session, certificate: Certificate) -> None:
    """Ставит удаление строки карты из Google Sheets в очередь."""
    enqueue_certificate_ids(db, [certificate.id])
    job = db.scalar(
        select(SheetsSyncJob).where(SheetsSyncJob.certificate_id == certificate.id)
    )
    if job is None:
        return
    job.job_kind = "delete"
    job.sheet_public_number = (certificate.public_number or "").strip()
    job.sheet_ledger_sheet = (certificate.ledger_sheet or "").strip()
    job.status = "pending"
    job.attempts = 0
    job.last_error = ""
    job.updated_at = _utcnow()
    db.commit()


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
                job_kind="sync",
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
        return {"written": 0, "skipped": 0, "errors": 0, "blocked": 1, "quota_wait": 0}

    if not sheets_enabled():
        return {"written": 0, "skipped": 0, "errors": 0, "blocked": 0, "quota_wait": 0}

    db = open_session()
    quota_hit = False
    written = 0
    errors = 0
    try:
        jobs = list(
            db.scalars(
                select(SheetsSyncJob)
                .where(SheetsSyncJob.status == "pending")
                .order_by(SheetsSyncJob.updated_at, SheetsSyncJob.id)
                .limit(limit)
            ).all()
        )
        if not jobs:
            return {"written": 0, "skipped": 0, "errors": 0, "blocked": 0, "quota_wait": 0}

        now = _utcnow()
        for job in jobs:
            if quota_hit:
                break
            kind = (job.job_kind or "sync").strip().lower()
            if kind == "delete":
                ok, retryable, err = _process_delete_job(job)
            else:
                ok, retryable, err = _process_sync_job(db, job)
            job.attempts += 1
            job.updated_at = now
            if ok:
                job.status = "done"
                job.last_error = ""
                written += 1
            else:
                job.last_error = (err or "Ошибка Google Sheets")[:500]
                if job.attempts >= _MAX_ATTEMPTS or not retryable:
                    job.status = "failed"
                    errors += 1
                else:
                    job.status = "pending"
                    quota_hit = retryable
                    errors += 1
            db.commit()
            if not quota_hit and job.status == "done":
                time.sleep(_SLEEP_BETWEEN_JOBS_SEC)

        if written or errors:
            logger.info(
                "Google Sheets очередь: готово %s, ошибок/отложено %s",
                written,
                errors,
            )
        return {
            "written": written,
            "skipped": 0,
            "errors": errors,
            "blocked": 0,
            "quota_wait": 1 if quota_hit else 0,
        }
    finally:
        db.close()


def _process_delete_job(job: SheetsSyncJob) -> tuple[bool, bool, str]:
    """Удаляет строку карты. (успех, можно повторить, сообщение)."""
    if not job.sheet_public_number.strip():
        return False, False, "Нет номера карты для удаления"
    ok = delete_certificate_row(
        job.sheet_public_number,
        job.sheet_ledger_sheet,
    )
    if ok:
        return True, False, ""
    return False, True, "Квота Google Sheets — удаление повторим позже"


def _process_sync_job(db: Session, job: SheetsSyncJob) -> tuple[bool, bool, str]:
    """Синхронизирует одну карту (профиль + расходы) в Google Sheets."""
    certificate = db.get(Certificate, job.certificate_id)
    if certificate is None:
        return False, False, "Сертификат не найден"
    holder = db.get(User, certificate.holder_id)
    if holder is None:
        return False, False, "Держатель не найден"
    if certificate.is_deleted:
        return False, False, "Карта удалена — нужна задача delete"
    segment_name = ""
    if certificate.segment is not None:
        segment_name = (certificate.segment.name or "").strip()
    expired = (
        certificate.expires_on is not None and certificate.expires_on < today_msk()
    )
    usages = [] if expired else list_sheet_usages(db, certificate.id)
    ok = sync_certificate_usage(
        certificate,
        holder,
        usages=usages,
        segment_name=segment_name,
        expired=expired,
    )
    if ok:
        return True, False, ""
    return False, True, "Квота Google Sheets — синк повторим позже"


async def sheets_queue_worker() -> None:
    """Фоновый цикл: последовательно дописывает очередь в Google Sheets."""
    while True:
        try:
            stats = await asyncio.to_thread(process_sheets_queue_once)
            if stats.get("blocked"):
                await asyncio.sleep(30.0)
                continue
            if stats.get("quota_wait"):
                await asyncio.sleep(_QUOTA_BACKOFF_SEC)
                continue
            if stats["written"] or stats["errors"]:
                await asyncio.sleep(_SLEEP_AFTER_WORK_SEC)
            else:
                await asyncio.sleep(_POLL_IDLE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой очереди Google Sheets")
            await asyncio.sleep(10.0)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

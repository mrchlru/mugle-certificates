"""Автоначисление бонусов по сегментам (ежедневно и ежемесячно)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.db import open_session
from bonus.models import AccrualPeriod, Certificate, CertificateStatus, Segment, Transaction, TxType
from bonus.services import BonusError, reset_monthly_balance
from bonus.values import MSK, as_utc, today_msk
from bonus.weekdays import WEEKDAYS, allows_weekday

logger = logging.getLogger("bonus.accrual")

_MONTHLY_PREFIX = "Автоначисление: ежемесячно"
_DAILY_PREFIX = "Автоначисление: ежедневно"


def run_scheduled_accruals() -> int:
    """Проверяет окно 04:00 МСК и начисляет положенные бонусы. Возвращает число карт."""
    db = open_session()
    try:
        now = datetime.now(MSK)
        total = 0
        if _accrual_window_open(now):
            total += apply_daily_accruals(db, now)
            total += apply_monthly_accruals(db, now)
        return total
    finally:
        db.close()


def apply_daily_accruals(db: Session, now: datetime | None = None) -> int:
    """Сбрасывает остаток к номиналу картам с ежедневным начислением в выбранные дни."""
    moment = now or datetime.now(MSK)
    marker = _daily_marker(moment)
    certificates = db.scalars(
        select(Certificate)
        .join(Segment, Certificate.segment_id == Segment.id)
        .where(
            Segment.accrual_period == AccrualPeriod.daily,
            Segment.is_active.is_(True),
            Certificate.status == CertificateStatus.active,
            Certificate.nominal > 0,
            Certificate.single_use.is_(False),
        )
        .order_by(Certificate.id)
    ).all()
    done = 0
    for certificate in certificates:
        if not _certificate_accepts_daily(certificate, moment):
            continue
        if _has_marker_credit(db, certificate.id, marker):
            continue
        try:
            reset_monthly_balance(db, certificate, marker)
        except BonusError as exc:
            logger.warning(
                "Ежедневное начисление %s пропущено: %s",
                certificate.public_number,
                exc.message,
            )
            continue
        done += 1
        logger.info(
            "Ежедневный сброс %s → номинал %s коп.",
            certificate.public_number,
            certificate.nominal,
        )
    if done:
        print(f"Teams Bonus: ежедневное начисление — {done} карт ({marker})", flush=True)
    return done


def apply_monthly_accruals(db: Session, now: datetime | None = None) -> int:
    """Сбрасывает остаток и ставит номинал картам с «ежемесячно», раз в календарный месяц.

    Карта, выпущенная внутри месяца (например 20.10), живёт до 1-го следующего месяца —
    тогда лимит обновляется. В месяц выпуска повторного начисления нет.
    """
    moment = now or datetime.now(MSK)
    marker = _monthly_marker(moment.year, moment.month)
    certificates = db.scalars(
        select(Certificate)
        .join(Segment, Certificate.segment_id == Segment.id)
        .where(
            Segment.accrual_period == AccrualPeriod.monthly,
            Segment.is_active.is_(True),
            Certificate.status == CertificateStatus.active,
            Certificate.nominal > 0,
            Certificate.single_use.is_(False),
        )
        .order_by(Certificate.id)
    ).all()
    done = 0
    for certificate in certificates:
        if not _certificate_accepts_monthly(certificate, moment):
            continue
        if _has_marker_credit(db, certificate.id, marker):
            continue
        try:
            reset_monthly_balance(db, certificate, marker)
        except BonusError as exc:
            logger.warning(
                "Ежемесячное начисление %s пропущено: %s",
                certificate.public_number,
                exc.message,
            )
            continue
        done += 1
        logger.info(
            "Ежемесячный сброс %s → номинал %s коп.",
            certificate.public_number,
            certificate.nominal,
        )
    if done:
        print(f"Teams Bonus: ежемесячное начисление — {done} карт ({marker})", flush=True)
    return done


async def accrual_worker() -> None:
    """Фоновый цикл: сразу догон, затем каждый день около 04:00 МСК."""
    while True:
        try:
            await asyncio.to_thread(run_scheduled_accruals)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой автоначисления")
        await asyncio.sleep(_seconds_until_next_moscow_4am())


def _accrual_window_open(now: datetime) -> bool:
    """После 04:00 МСК можно (идемпотентно) начислять за сегодня / месяц."""
    return (now.hour, now.minute, now.second) >= (4, 0, 0)


def _seconds_until_next_moscow_4am() -> float:
    """Сколько спать до следующего 04:00 Europe/Moscow."""
    now = datetime.now(MSK)
    target = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return max(30.0, (target - now).total_seconds())


def _monthly_marker(year: int, month: int) -> str:
    return f"{_MONTHLY_PREFIX} {year:04d}-{month:02d}"


def _daily_marker(moment: datetime) -> str:
    day = moment.astimezone(MSK).date()
    return f"{_DAILY_PREFIX} {day.isoformat()}"


def _certificate_accepts_daily(certificate: Certificate, moment: datetime) -> bool:
    """День в маске начисления, не день выпуска, не просрочена."""
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return False
    if not allows_weekday(certificate.accrual_days, moment.isoweekday(), default=WEEKDAYS):
        return False
    created = as_utc(certificate.created_at).astimezone(MSK).date()
    if created >= moment.astimezone(MSK).date():
        return False
    return True


def _certificate_accepts_monthly(certificate: Certificate, moment: datetime) -> bool:
    """Не просрочена; в месяц выпуска автосброс не делается — лимит уже выдан при выпуске."""
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return False
    created = as_utc(certificate.created_at).astimezone(MSK)
    if (created.year, created.month) >= (moment.year, moment.month):
        return False
    return True


def _has_marker_credit(db: Session, certificate_id: int, marker: str) -> bool:
    found = db.scalar(
        select(Transaction.id).where(
            Transaction.certificate_id == certificate_id,
            Transaction.tx_type == TxType.credit,
            Transaction.comment == marker,
        )
    )
    return found is not None

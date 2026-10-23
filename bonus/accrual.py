"""Автоначисление и дневное сгорание ежедневных бонусов."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.db import open_session
from bonus.models import AccrualPeriod, Certificate, CertificateStatus, Segment, Transaction, TxType
from bonus.services import BonusError, burn_daily_remainder, reset_monthly_balance
from bonus.values import MSK, as_utc, today_msk
from bonus.weekdays import WEEKDAYS, allows_weekday

logger = logging.getLogger("bonus.accrual")

_MONTHLY_PREFIX = "Автоначисление: ежемесячно"
_WEEKLY_PREFIX = "Автоначисление: еженедельно"
_DAILY_PREFIX = "Автоначисление: ежедневно"
_DAILY_BURN_PREFIX = "Сгорание остатка: ежедневно"


def run_scheduled_accruals() -> int:
    """Окно 04:00 — начисления; окно 17:00 — сгорание дневного остатка."""
    db = open_session()
    try:
        now = datetime.now(MSK)
        total = 0
        if _accrual_window_open(now):
            total += apply_daily_accruals(db, now)
            total += apply_weekly_accruals(db, now)
            total += apply_monthly_accruals(db, now)
        if _daily_burn_window_open(now):
            total += apply_daily_burns(db, now)
        return total
    finally:
        db.close()


def apply_daily_accruals(db: Session, now: datetime | None = None) -> int:
    """В 04:00 МСК выставляет номинал картам с ежедневным начислением в выбранные дни."""
    moment = now or datetime.now(MSK)
    marker = _daily_marker(moment)
    certificates = _daily_certificates(db)
    done = 0
    for certificate in certificates:
        if not _certificate_accepts_daily(certificate, moment):
            continue
        if _has_marker(db, certificate.id, TxType.credit, marker):
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
            "Ежедневное начисление %s → номинал %s коп.",
            certificate.public_number,
            certificate.nominal,
        )
    if done:
        print(f"Teams Bonus: ежедневное начисление — {done} карт ({marker})", flush=True)
    return done


def apply_daily_burns(db: Session, now: datetime | None = None) -> int:
    """В 17:00 МСК сжигает неиспользованный остаток ежедневных карт."""
    moment = now or datetime.now(MSK)
    marker = _daily_burn_marker(moment)
    certificates = _daily_certificates(db)
    done = 0
    for certificate in certificates:
        if certificate.expires_on is not None and certificate.expires_on < today_msk():
            continue
        if _has_marker(db, certificate.id, TxType.burn, marker):
            continue
        try:
            burn_daily_remainder(db, certificate, marker)
        except BonusError as exc:
            logger.warning(
                "Ежедневное сгорание %s пропущено: %s",
                certificate.public_number,
                exc.message,
            )
            continue
        done += 1
        logger.info("Ежедневное сгорание %s", certificate.public_number)
    if done:
        print(f"Teams Bonus: ежедневное сгорание — {done} карт ({marker})", flush=True)
    return done


def apply_weekly_accruals(db: Session, now: datetime | None = None) -> int:
    """Сбрасывает остаток и ставит номинал картам с «еженедельно», раз в ISO-неделю.

    Как у ежемесячных: не накопительные, баланс обновляется к номиналу в 04:00 МСК
    (после начала новой недели, обычно с понедельника). В неделю выпуска повторного
    начисления нет — лимит уже выдан при выпуске.
    """
    moment = now or datetime.now(MSK)
    year, week = _iso_year_week(moment)
    marker = _weekly_marker(year, week)
    certificates = db.scalars(
        select(Certificate)
        .join(Segment, Certificate.segment_id == Segment.id)
        .where(
            Segment.accrual_period == AccrualPeriod.weekly,
            Segment.is_active.is_(True),
            Certificate.status == CertificateStatus.active,
            Certificate.nominal > 0,
            Certificate.single_use.is_(False),
        )
        .order_by(Certificate.id)
    ).all()
    done = 0
    for certificate in certificates:
        if not _certificate_accepts_weekly(certificate, moment):
            continue
        if _has_marker(db, certificate.id, TxType.credit, marker):
            continue
        try:
            reset_monthly_balance(db, certificate, marker)
        except BonusError as exc:
            logger.warning(
                "Еженедельное начисление %s пропущено: %s",
                certificate.public_number,
                exc.message,
            )
            continue
        done += 1
        logger.info(
            "Еженедельный сброс %s → номинал %s коп.",
            certificate.public_number,
            certificate.nominal,
        )
    if done:
        print(f"Teams Bonus: еженедельное начисление — {done} карт ({marker})", flush=True)
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
        if _has_marker(db, certificate.id, TxType.credit, marker):
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
    """Фоновый цикл: догон, затем сон до ближайших 04:00 или 17:00 МСК."""
    while True:
        try:
            await asyncio.to_thread(run_scheduled_accruals)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой автоначисления")
        await asyncio.sleep(_seconds_until_next_job())


def _daily_certificates(db: Session) -> list[Certificate]:
    return list(
        db.scalars(
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
    )


def _accrual_window_open(now: datetime) -> bool:
    """После 04:00 МСК можно (идемпотентно) начислять за сегодня / неделю / месяц."""
    return (now.hour, now.minute, now.second) >= (4, 0, 0)


def _daily_burn_window_open(now: datetime) -> bool:
    """После 17:00 МСК можно (идемпотентно) сжечь дневной остаток."""
    return (now.hour, now.minute, now.second) >= (17, 0, 0)


def _seconds_until_next_job() -> float:
    """Сон до ближайших 04:00 или 17:00 Europe/Moscow."""
    now = datetime.now(MSK)
    targets = [
        now.replace(hour=4, minute=0, second=0, microsecond=0),
        now.replace(hour=17, minute=0, second=0, microsecond=0),
    ]
    upcoming: list[datetime] = []
    for target in targets:
        point = target
        if now >= point:
            point = point + timedelta(days=1)
        upcoming.append(point)
    return max(30.0, (min(upcoming) - now).total_seconds())


def _monthly_marker(year: int, month: int) -> str:
    return f"{_MONTHLY_PREFIX} {year:04d}-{month:02d}"


def _weekly_marker(year: int, week: int) -> str:
    return f"{_WEEKLY_PREFIX} {year:04d}-W{week:02d}"


def _daily_marker(moment: datetime) -> str:
    day = moment.astimezone(MSK).date()
    return f"{_DAILY_PREFIX} {day.isoformat()}"


def _daily_burn_marker(moment: datetime) -> str:
    day = moment.astimezone(MSK).date()
    return f"{_DAILY_BURN_PREFIX} {day.isoformat()}"


def _iso_year_week(moment: datetime) -> tuple[int, int]:
    """ISO-год и номер недели (пн — начало недели)."""
    day = moment.astimezone(MSK).date()
    iso = day.isocalendar()
    return int(iso.year), int(iso.week)


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


def _certificate_accepts_weekly(certificate: Certificate, moment: datetime) -> bool:
    """Не просрочена; в неделю выпуска автосброс не делается — лимит уже выдан при выпуске."""
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return False
    created = as_utc(certificate.created_at).astimezone(MSK)
    created_yw = _iso_year_week(created)
    now_yw = _iso_year_week(moment)
    if created_yw >= now_yw:
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


def _has_marker(db: Session, certificate_id: int, tx_type: TxType, marker: str) -> bool:
    found = db.scalar(
        select(Transaction.id).where(
            Transaction.certificate_id == certificate_id,
            Transaction.tx_type == tx_type,
            Transaction.comment == marker,
        )
    )
    return found is not None

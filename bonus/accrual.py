"""Автоначисление и дневное сгорание по частям карт."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.db import open_session, release_accrual_cluster_lock, try_accrual_cluster_lock
from bonus.models import (
    AccrualPeriod,
    Certificate,
    CertificatePart,
    CertificateStatus,
    Transaction,
    TxType,
)
from bonus.services import BonusError, burn_part_remainder, reset_part_balance
from bonus.values import MSK, as_utc, today_msk
from bonus.weekdays import WEEKDAYS, allows_weekday

logger = logging.getLogger("bonus.accrual")

_MONTHLY_PREFIX = "Автоначисление: ежемесячно"
_WEEKLY_PREFIX = "Автоначисление: еженедельно"
_DAILY_PREFIX = "Автоначисление: ежедневно"
_DAILY_BURN_PREFIX = "Сгорание остатка: ежедневно"
_WEEKLY_BURN_PREFIX = "Сгорание остатка: еженедельно"


def run_scheduled_accruals() -> int:
    """Окно 04:00 — начисления; с 23:59 — сгорание daily/weekly остатка."""
    db = open_session()
    try:
        if not try_accrual_cluster_lock(db):
            logger.info("Автоначисление: другой процесс уже выполняет задание")
            return 0
        try:
            now = datetime.now(MSK)
            total = 0
            if _accrual_window_open(now):
                total += apply_daily_accruals(db, now)
                total += apply_weekly_accruals(db, now)
                total += apply_monthly_accruals(db, now)
            if _daily_burn_window_open(now):
                total += apply_daily_burns(db, now)
                total += apply_weekly_burns(db, now)
            elif _weekly_burn_catchup_open(now):
                # После дня сгорания догоняем пропущенный 23:59 до пн.
                total += apply_weekly_burns(db, now)
            return total
        finally:
            release_accrual_cluster_lock(db)
    finally:
        db.close()


def apply_daily_accruals(db: Session, now: datetime | None = None) -> int:
    """В 04:00 МСК выставляет номинал ежедневным частям в выбранные дни."""
    moment = now or datetime.now(MSK)
    marker = _daily_marker(moment)
    done = 0
    for part, certificate in _parts_with_period(db, AccrualPeriod.daily):
        if not _part_accepts_daily(part, certificate, moment):
            continue
        if not _accrue_part_if_needed(db, part, certificate, TxType.credit, marker):
            continue
        try:
            reset_part_balance(db, part, marker)
        except BonusError as exc:
            logger.warning(
                "Ежедневное начисление %s/%s пропущено: %s",
                certificate.public_number,
                part.segment_name,
                exc.message,
            )
            continue
        done += 1
        logger.info(
            "Ежедневное начисление %s · %s → номинал %s коп.",
            certificate.public_number,
            part.segment_name,
            part.nominal,
        )
    if done:
        print(f"Teams Bonus: ежедневное начисление — {done} частей ({marker})", flush=True)
    return done


def apply_daily_burns(db: Session, now: datetime | None = None) -> int:
    """В 23:59 МСК сжигает неиспользованный остаток ежедневных частей (ланч)."""
    moment = now or datetime.now(MSK)
    marker = _daily_burn_marker(moment)
    done = 0
    for part, certificate in _parts_with_period(db, AccrualPeriod.daily):
        if certificate.expires_on is not None and certificate.expires_on < today_msk():
            continue
        if not _burn_part_if_needed(db, part, certificate, marker):
            continue
        try:
            burn_part_remainder(db, part, marker)
        except BonusError as exc:
            logger.warning(
                "Ежедневное сгорание %s/%s пропущено: %s",
                certificate.public_number,
                part.segment_name,
                exc.message,
            )
            continue
        done += 1
        logger.info(
            "Ежедневное сгорание %s · %s",
            certificate.public_number,
            part.segment_name,
        )
    if done:
        print(f"Teams Bonus: ежедневное сгорание — {done} частей ({marker})", flush=True)
    return done


def apply_weekly_accruals(db: Session, now: datetime | None = None) -> int:
    """Сброс к номиналу еженедельных частей (понедельник 04:00 МСК)."""
    moment = now or datetime.now(MSK)
    year, week = _iso_year_week(moment)
    marker = _weekly_marker(year, week)
    done = 0
    for part, certificate in _parts_with_period(db, AccrualPeriod.weekly):
        if not _part_accepts_weekly(certificate, moment):
            continue
        if not _accrue_part_if_needed(db, part, certificate, TxType.credit, marker):
            continue
        try:
            reset_part_balance(db, part, marker)
        except BonusError as exc:
            logger.warning(
                "Еженедельное начисление %s/%s пропущено: %s",
                certificate.public_number,
                part.segment_name,
                exc.message,
            )
            continue
        done += 1
        logger.info(
            "Еженедельный сброс %s · %s → номинал %s коп.",
            certificate.public_number,
            part.segment_name,
            part.nominal,
        )
    if done:
        print(
            f"Teams Bonus: еженедельное начисление — {done} частей ({marker})",
            flush=True,
        )
    return done


def apply_weekly_burns(db: Session, now: datetime | None = None) -> int:
    """Сжигает остаток еженедельных частей в выбранный день (23:59 МСК).

    День сгорания хранится в part.accrual_days (один ISO-день). Начисление — пн.
    """
    from bonus.weekdays import normalize_single_weekday

    moment = now or datetime.now(MSK)
    year, week = _iso_year_week(moment)
    marker = _weekly_burn_marker(year, week)
    done = 0
    for part, certificate in _parts_with_period(db, AccrualPeriod.weekly):
        if certificate.expires_on is not None and certificate.expires_on < today_msk():
            continue
        burn_day = int(normalize_single_weekday(part.accrual_days, default="5"))
        if not _weekly_burn_due(moment, burn_day):
            continue
        if not _burn_part_if_needed(db, part, certificate, marker):
            continue
        try:
            burn_part_remainder(db, part, marker)
        except BonusError as exc:
            logger.warning(
                "Еженедельное сгорание %s/%s пропущено: %s",
                certificate.public_number,
                part.segment_name,
                exc.message,
            )
            continue
        done += 1
        logger.info(
            "Еженедельное сгорание %s · %s (день %s)",
            certificate.public_number,
            part.segment_name,
            burn_day,
        )
    if done:
        print(
            f"Teams Bonus: еженедельное сгорание — {done} частей ({marker})",
            flush=True,
        )
    return done


def apply_monthly_accruals(db: Session, now: datetime | None = None) -> int:
    """Сброс к номиналу ежемесячных частей (1-е число 04:00 МСК)."""
    moment = now or datetime.now(MSK)
    marker = _monthly_marker(moment.year, moment.month)
    done = 0
    for part, certificate in _parts_with_period(db, AccrualPeriod.monthly):
        if not _part_accepts_monthly(certificate, moment):
            continue
        if not _accrue_part_if_needed(db, part, certificate, TxType.credit, marker):
            continue
        try:
            reset_part_balance(db, part, marker)
        except BonusError as exc:
            logger.warning(
                "Ежемесячное начисление %s/%s пропущено: %s",
                certificate.public_number,
                part.segment_name,
                exc.message,
            )
            continue
        done += 1
        logger.info(
            "Ежемесячный сброс %s · %s → номинал %s коп.",
            certificate.public_number,
            part.segment_name,
            part.nominal,
        )
    if done:
        print(
            f"Teams Bonus: ежемесячное начисление — {done} частей ({marker})",
            flush=True,
        )
    return done


async def accrual_worker() -> None:
    """Фоновый цикл: догон, затем сон до ближайших 04:00 или 23:59 МСК."""
    while True:
        try:
            await asyncio.to_thread(run_scheduled_accruals)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Сбой автоначисления")
        await asyncio.sleep(_seconds_until_next_job())


def _parts_with_period(
    db: Session, period: AccrualPeriod
) -> list[tuple[CertificatePart, Certificate]]:
    """Активные части с заданным периодом начисления и положительным номиналом."""
    rows = db.execute(
        select(CertificatePart, Certificate)
        .join(Certificate, CertificatePart.certificate_id == Certificate.id)
        .where(
            CertificatePart.accrual_period == period,
            CertificatePart.nominal > 0,
            Certificate.status == CertificateStatus.active,
            Certificate.single_use.is_(False),
        )
        .order_by(CertificatePart.id)
    ).all()
    return [(part, certificate) for part, certificate in rows]


def _accrual_window_open(now: datetime) -> bool:
    """После 04:00 МСК можно (идемпотентно) начислять за сегодня / неделю / месяц."""
    return (now.hour, now.minute, now.second) >= (4, 0, 0)


def _daily_burn_window_open(now: datetime) -> bool:
    """С 23:59 МСК — сгорание дневного остатка ланча за текущие сутки."""
    return (now.hour, now.minute, now.second) >= (23, 59, 0)


def _weekly_burn_catchup_open(now: datetime) -> bool:
    """Вне 23:59: догон еженедельного сгорания во вт–вс после дня списания."""
    return now.isoweekday() >= 2


def _weekly_burn_due(moment: datetime, burn_day: int) -> bool:
    """Пора ли сжигать weekly: в день сгорания с 23:59 или позже на неделе."""
    today = moment.isoweekday()
    if today < burn_day:
        return False
    if today == burn_day:
        return _daily_burn_window_open(moment)
    return True


def _seconds_until_next_job() -> float:
    """Сон до ближайших 04:00 или 23:59 Europe/Moscow."""
    now = datetime.now(MSK)
    targets = [
        now.replace(hour=4, minute=0, second=0, microsecond=0),
        now.replace(hour=23, minute=59, second=0, microsecond=0),
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


def _weekly_burn_marker(year: int, week: int) -> str:
    return f"{_WEEKLY_BURN_PREFIX} {year:04d}-W{week:02d}"


def _iso_year_week(moment: datetime) -> tuple[int, int]:
    """ISO-год и номер недели (пн — начало недели)."""
    day = moment.astimezone(MSK).date()
    iso = day.isocalendar()
    return int(iso.year), int(iso.week)


def _part_accepts_daily(
    part: CertificatePart, certificate: Certificate, moment: datetime
) -> bool:
    """День в маске начисления части, не день выпуска, не просрочена."""
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return False
    if not allows_weekday(part.accrual_days, moment.isoweekday(), default=WEEKDAYS):
        return False
    created = as_utc(certificate.created_at).astimezone(MSK).date()
    if created >= moment.astimezone(MSK).date():
        return False
    return True


def _part_accepts_weekly(certificate: Certificate, moment: datetime) -> bool:
    """Не просрочена; в неделю выпуска автосброс не делается."""
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return False
    created = as_utc(certificate.created_at).astimezone(MSK)
    if _iso_year_week(created) >= _iso_year_week(moment):
        return False
    return True


def _part_accepts_monthly(certificate: Certificate, moment: datetime) -> bool:
    """Не просрочена; в месяц выпуска автосброс не делается."""
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return False
    created = as_utc(certificate.created_at).astimezone(MSK)
    if (created.year, created.month) >= (moment.year, moment.month):
        return False
    return True


def _burn_part_if_needed(
    db: Session,
    part: CertificatePart,
    certificate: Certificate,
    marker: str,
) -> bool:
    """Проверяет маркер сгорания и лочит часть."""
    if _has_marker(db, certificate.id, part.id, TxType.burn, marker):
        return False
    row = db.execute(
        select(CertificatePart.id)
        .where(
            CertificatePart.id == part.id,
            CertificatePart.certificate_id == certificate.id,
        )
        .with_for_update()
    ).scalar()
    if row is None:
        return False
    db.flush()
    if _has_marker(db, certificate.id, part.id, TxType.burn, marker):
        return False
    return True


def _accrue_part_if_needed(
    db: Session,
    part: CertificatePart,
    certificate: Certificate,
    tx_type: TxType,
    marker: str,
) -> bool:
    """Проверяет маркер и лочит строку части перед автоначислением."""
    if _has_marker(db, certificate.id, part.id, tx_type, marker):
        return False
    row = db.execute(
        select(CertificatePart.id)
        .where(
            CertificatePart.id == part.id,
            CertificatePart.certificate_id == certificate.id,
        )
        .with_for_update()
    ).scalar()
    if row is None:
        return False
    db.flush()
    if _has_marker(db, certificate.id, part.id, tx_type, marker):
        return False
    return True


def _has_marker(
    db: Session,
    certificate_id: int,
    part_id: int,
    tx_type: TxType,
    marker: str,
) -> bool:
    found = db.scalar(
        select(Transaction.id).where(
            Transaction.certificate_id == certificate_id,
            Transaction.part_id == part_id,
            Transaction.tx_type == tx_type,
            Transaction.comment == marker,
        )
    )
    if found is not None:
        return True
    # Совместимость со старыми маркерами без part_id (до миграции частей).
    legacy = db.scalar(
        select(Transaction.id).where(
            Transaction.certificate_id == certificate_id,
            Transaction.part_id.is_(None),
            Transaction.tx_type == tx_type,
            Transaction.comment == marker,
        )
    )
    return legacy is not None

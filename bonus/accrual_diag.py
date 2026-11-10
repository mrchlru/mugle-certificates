"""Диагностика ежедневного автоначисления для админки."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bonus.accrual import (
    _daily_marker,
    _has_marker,
    _part_accepts_daily,
    _parts_with_period,
)
from bonus.config import get_settings
from bonus.models import AccrualPeriod, Transaction, TxType
from bonus.values import MSK, as_utc, today_msk
from bonus.weekdays import WEEKDAYS, allows_weekday


@dataclass(frozen=True)
class DailyAccrualDiagnostic:
    """Снимок состояния дневного автоначисления на текущий момент МСК."""

    now_msk: str
    weekday: int
    marker: str
    scheduler_enabled: bool
    credits_with_marker: int
    daily_parts_total: int
    eligible_today: int
    pending_accrual: int
    skip_weekday: int
    skip_expired: int
    skip_issued_today: int
    pending_samples: tuple[str, ...]


def daily_accrual_diagnostic(db: Session) -> DailyAccrualDiagnostic:
    """Считает, сколько дневных частей должны были пополниться сегодня."""
    moment = datetime.now(MSK)
    marker = _daily_marker(moment)
    settings = get_settings()
    credits = int(
        db.scalar(
            select(func.count()).select_from(Transaction).where(
                Transaction.tx_type == TxType.credit,
                Transaction.comment == marker,
                Transaction.reversal_of_id.is_(None),
            )
        )
        or 0
    )
    parts = _parts_with_period(db, AccrualPeriod.daily)
    skip_weekday = 0
    skip_expired = 0
    skip_issued_today = 0
    eligible = 0
    pending = 0
    samples: list[str] = []
    for part, certificate in parts:
        if not allows_weekday(part.accrual_days, moment.isoweekday(), default=WEEKDAYS):
            skip_weekday += 1
            continue
        if certificate.expires_on is not None and certificate.expires_on < today_msk():
            skip_expired += 1
            continue
        created = as_utc(certificate.created_at).astimezone(MSK).date()
        if created >= moment.astimezone(MSK).date():
            skip_issued_today += 1
            continue
        eligible += 1
        if _part_accepts_daily(part, certificate, moment) and not _has_marker(
            db, certificate.id, part.id, TxType.credit, marker
        ):
            pending += 1
            if len(samples) < 8:
                label = certificate.public_number
                if (part.segment_name or "").strip():
                    label = f"{label} · {part.segment_name.strip()}"
                samples.append(label)
    return DailyAccrualDiagnostic(
        now_msk=moment.strftime("%d.%m.%Y %H:%M"),
        weekday=moment.isoweekday(),
        marker=marker,
        scheduler_enabled=settings.accrual_scheduler_enabled,
        credits_with_marker=credits,
        daily_parts_total=len(parts),
        eligible_today=eligible,
        pending_accrual=pending,
        skip_weekday=skip_weekday,
        skip_expired=skip_expired,
        skip_issued_today=skip_issued_today,
        pending_samples=tuple(samples),
    )

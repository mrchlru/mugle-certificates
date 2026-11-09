"""Разовое восстановление остатков после отмены дубля автоначисления."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.accrual import (
    _daily_marker,
    _has_marker,
    _iso_year_week,
    _monthly_marker,
    _parts_with_period,
    _weekly_marker,
)
from bonus.models import AccrualPeriod, CertificateStatus, Transaction, TxType, User
from bonus.services import BonusError, _is_reversed, credit_certificate
from bonus.values import MSK


@dataclass(frozen=True)
class AccrualRepairResult:
    """Итог массового восстановления остатков за текущий период."""

    fixed: int
    skipped: int
    markers: tuple[str, ...]


def repair_zero_after_duplicate_undo(db: Session, actor: User) -> AccrualRepairResult:
    """Доводит части с нулём до номинала, если автоначисление за период уже было.

    Сценарий: в журнале два credit с одним маркером, на балансе фактически один лимит;
    отмена дубля ошибочно обнулила часть. Со следующей недели/дня помогает умная отмена.
    """
    moment = datetime.now(MSK)
    markers: list[str] = []
    fixed = 0
    skipped = 0
    for period, marker in _current_markers(moment):
        markers.append(marker)
        for part, certificate in _parts_with_period(db, period):
            if certificate.status != CertificateStatus.active:
                skipped += 1
                continue
            if part.nominal <= 0 or part.balance > 0:
                skipped += 1
                continue
            if not _has_marker(db, certificate.id, part.id, TxType.credit, marker):
                skipped += 1
                continue
            if not _live_accrual_credit(db, certificate.id, part.id, marker):
                skipped += 1
                continue
            credit_certificate(
                db,
                certificate,
                actor,
                part.nominal,
                f"Восстановление после отмены дубля ({marker})",
                grow_nominal=False,
                part_id=part.id,
            )
            fixed += 1
    return AccrualRepairResult(fixed=fixed, skipped=skipped, markers=tuple(markers))


def sibling_active_accrual_credit(db: Session, tx: Transaction) -> bool:
    """Есть ли другое неотменённое начисление с тем же маркером периода."""
    comment = (tx.comment or "").strip()
    if not comment.startswith("Автоначисление:"):
        return False
    rows = db.scalars(
        select(Transaction).where(
            Transaction.id != tx.id,
            Transaction.certificate_id == tx.certificate_id,
            Transaction.part_id == tx.part_id,
            Transaction.tx_type == TxType.credit,
            Transaction.comment == comment,
        )
    ).all()
    return any(not _is_reversed(db, row.id) for row in rows)


def _live_accrual_credit(db: Session, certificate_id: int, part_id: int, marker: str) -> bool:
    rows = db.scalars(
        select(Transaction).where(
            Transaction.certificate_id == certificate_id,
            Transaction.part_id == part_id,
            Transaction.tx_type == TxType.credit,
            Transaction.comment == marker,
        )
    ).all()
    return any(not _is_reversed(db, row.id) for row in rows)


def _current_markers(moment: datetime) -> list[tuple[AccrualPeriod, str]]:
    items: list[tuple[AccrualPeriod, str]] = [
        (AccrualPeriod.daily, _daily_marker(moment)),
    ]
    year, week = _iso_year_week(moment)
    items.append((AccrualPeriod.weekly, _weekly_marker(year, week)))
    items.append(
        (AccrualPeriod.monthly, _monthly_marker(moment.year, moment.month)),
    )
    return items

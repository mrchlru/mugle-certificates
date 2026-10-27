"""Части комбинированных карт: остатки по сегментам и порядок списания."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from bonus.models import AccrualPeriod, Certificate, CertificatePart, Segment
from bonus.values import today_msk
from bonus.weekdays import allows_weekday

# Сначала сгорающее (ежедневное), потом более «долгое».
_SPEND_ORDER = {
    AccrualPeriod.daily: 0,
    AccrualPeriod.weekly: 1,
    AccrualPeriod.monthly: 2,
    AccrualPeriod.yearly: 3,
    AccrualPeriod.none: 4,
}


@dataclass(frozen=True)
class PartDraft:
    """Номинал и дни одной части при выпуске."""

    segment_id: int
    nominal: int
    use_days: str = "1234567"
    accrual_days: str = "12345"
    balance: int | None = None
    sort_order: int = 1


def list_parts(db: Session, certificate_id: int) -> list[CertificatePart]:
    """Части карты по порядку sort_order."""
    return list(
        db.scalars(
            select(CertificatePart)
            .where(CertificatePart.certificate_id == certificate_id)
            .order_by(CertificatePart.sort_order, CertificatePart.id)
        ).all()
    )


def sync_certificate_totals(db: Session, certificate_id: int) -> None:
    """Выставляет certificate.balance/nominal = сумма частей."""
    parts = list_parts(db, certificate_id)
    total_balance = sum(max(0, part.balance) for part in parts)
    total_nominal = sum(max(0, part.nominal) for part in parts)
    db.execute(
        update(Certificate)
        .where(Certificate.id == certificate_id)
        .values(balance=total_balance, nominal=total_nominal)
    )


def available_balance(
    parts: list[CertificatePart],
    *,
    enforce_weekdays: bool = True,
) -> int:
    """Сумма остатков частей, которыми можно платить сегодня."""
    today = today_msk().isoweekday()
    total = 0
    for part in parts:
        if part.balance <= 0:
            continue
        if enforce_weekdays and not allows_weekday(part.use_days, today):
            continue
        total += part.balance
    return total


def spend_order(parts: list[CertificatePart]) -> list[CertificatePart]:
    """Порядок списания: daily → weekly → monthly → yearly → none."""
    return sorted(
        parts,
        key=lambda part: (
            _SPEND_ORDER.get(part.accrual_period, 9),
            part.sort_order,
            part.id,
        ),
    )


def change_part_balance(
    db: Session,
    part_id: int,
    delta: int,
) -> bool:
    """Атомарно меняет остаток части. False, если не хватает при списании."""
    statement = update(CertificatePart).where(CertificatePart.id == part_id)
    if delta < 0:
        statement = statement.where(CertificatePart.balance >= -delta)
    result = db.execute(
        statement.values(balance=CertificatePart.balance + delta)
    )
    return result.rowcount == 1


def create_parts_for_certificate(
    db: Session,
    certificate: Certificate,
    drafts: list[PartDraft],
) -> list[CertificatePart]:
    """Создаёт части при выпуске; нулевой номинал пропускает."""
    created: list[CertificatePart] = []
    for index, draft in enumerate(drafts, start=1):
        if draft.nominal <= 0 and (draft.balance is None or draft.balance <= 0):
            continue
        segment = db.get(Segment, draft.segment_id)
        if segment is None:
            raise ValueError(f"Сегмент {draft.segment_id} не найден")
        opening = draft.balance if draft.balance is not None else draft.nominal
        opening = max(0, opening)
        nominal = max(draft.nominal, opening)
        part = CertificatePart(
            certificate_id=certificate.id,
            segment_id=segment.id,
            sort_order=draft.sort_order or index,
            segment_name=(segment.name or "").strip()[:120],
            accrual_period=segment.accrual_period or AccrualPeriod.none,
            use_days=(draft.use_days or segment.use_days or "1234567")[:16],
            accrual_days=(draft.accrual_days or segment.accrual_days or "12345")[:16],
            nominal=nominal,
            balance=opening,
        )
        db.add(part)
        created.append(part)
    db.flush()
    sync_certificate_totals(db, certificate.id)
    return created


def ensure_legacy_part(db: Session, certificate: Certificate) -> CertificatePart | None:
    """Если у карты ещё нет частей — создаёт одну из текущих полей сертификата."""
    existing = list_parts(db, certificate.id)
    if existing:
        return existing[0]
    if certificate.segment_id is None:
        return None
    segment = db.get(Segment, certificate.segment_id)
    period = AccrualPeriod.none
    name = ""
    if segment is not None:
        period = segment.accrual_period or AccrualPeriod.none
        name = (segment.name or "").strip()[:120]
    part = CertificatePart(
        certificate_id=certificate.id,
        segment_id=certificate.segment_id,
        sort_order=1,
        segment_name=name,
        accrual_period=period,
        use_days=(certificate.use_days or "1234567")[:16],
        accrual_days=(certificate.accrual_days or "12345")[:16],
        nominal=max(0, certificate.nominal),
        balance=max(0, certificate.balance),
    )
    db.add(part)
    db.flush()
    return part


def unwrap_kind_from_segments(db: Session, segment_ids: list[int | None]) -> str:
    """Первое непустое unwrap_kind среди сегментов шаблона."""
    for segment_id in segment_ids:
        if segment_id is None or segment_id <= 0:
            continue
        segment = db.get(Segment, segment_id)
        if segment is None:
            continue
        kind = (getattr(segment, "unwrap_kind", None) or "").strip().lower()
        if kind in {"birthday", "gift"}:
            return kind
        if getattr(segment, "birthday_unwrap", False):
            return "birthday"
    return ""

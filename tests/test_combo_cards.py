"""Комбинированные карты: две части, списание daily→monthly, начисление."""

from datetime import datetime

import pytest

import bonus.db as database
from bonus.accrual import (
    apply_daily_burns,
    apply_weekly_accruals,
    apply_weekly_burns,
)
from bonus.catalog import create_segment, ensure_default_segments, list_segments, save_template
from bonus.models import AccrualPeriod, Base, CertificatePart, Role, Transaction, TxType, User
from bonus.parts import PartDraft, list_parts
from bonus.security import hash_password
from bonus.services import BonusError, IssueDraft, debit_certificate, issue_certificate
from bonus.values import MSK
from sqlalchemy import select


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def test_combo_issue_and_debit_order() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager = _person(db, "mgr", Role.manager)
    ensure_default_segments(db)
    lunch = next(item for item in list_segments(db) if item.name == "Ланч")
    monthly = next(
        item for item in list_segments(db) if item.name == "Ежемесячные бонусы"
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Комбо Тест",
            login="",
            password="",
            nominal=900_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=lunch.id,
            parts=(
                PartDraft(
                    segment_id=lunch.id,
                    nominal=400_00,
                    use_days="12345",
                    accrual_days="12345",
                    sort_order=1,
                ),
                PartDraft(
                    segment_id=monthly.id,
                    nominal=500_00,
                    use_days="1234567",
                    accrual_days="12345",
                    sort_order=2,
                ),
            ),
        ),
        hr,
        sync_external=False,
    )
    parts = list_parts(db, issued.certificate.id)
    assert len(parts) == 2
    assert issued.certificate.balance == 900_00
    assert sum(part.balance for part in parts) == 900_00

    debit_certificate(db, issued.certificate, manager, 600_00, "обед+", enforce_weekdays=False)
    db.refresh(issued.certificate)
    parts = list_parts(db, issued.certificate.id)
    by_period = {part.accrual_period: part for part in parts}
    assert by_period[AccrualPeriod.daily].balance == 0
    assert by_period[AccrualPeriod.monthly].balance == 300_00
    assert issued.certificate.balance == 300_00

    debits = db.scalars(
        select_debits(db, issued.certificate.id)
    ).all()
    assert len(debits) == 2
    assert {tx.amount for tx in debits} == {400_00, 200_00}


def test_daily_burn_leaves_monthly() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    ensure_default_segments(db)
    lunch = next(item for item in list_segments(db) if item.name == "Ланч")
    monthly = next(
        item for item in list_segments(db) if item.name == "Ежемесячные бонусы"
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Сгорание",
            login="",
            password="",
            nominal=700_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=lunch.id,
            parts=(
                PartDraft(segment_id=lunch.id, nominal=200_00, sort_order=1),
                PartDraft(segment_id=monthly.id, nominal=500_00, sort_order=2),
            ),
        ),
        hr,
        sync_external=False,
    )
    # Сдвигаем выпуск в прошлое, чтобы daily accrual/burn принимали карту.
    issued.certificate.created_at = datetime(2026, 10, 1, 10, 0, tzinfo=MSK)
    db.commit()

    moment = datetime(2026, 10, 27, 23, 59, tzinfo=MSK)
    burned = apply_daily_burns(db, moment)
    assert burned >= 1
    db.refresh(issued.certificate)
    parts = {part.accrual_period: part for part in list_parts(db, issued.certificate.id)}
    assert parts[AccrualPeriod.daily].balance == 0
    assert parts[AccrualPeriod.monthly].balance == 500_00
    assert issued.certificate.balance == 500_00

    # Повторный burn в тот же день не меняет баланс.
    apply_daily_burns(db, moment)
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 500_00


def test_weekly_accrual_monday_burn_friday() -> None:
    """Пн 04:00 — начисление; выбранный день (пт) 23:59 — сгорание до следующего пн."""
    db = _db()
    hr = _person(db, "hr", Role.hr)
    segment = create_segment(
        db,
        "Еженедельный тест",
        AccrualPeriod.weekly,
        accrual_days="5",
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Неделя Тест",
            login="",
            password="",
            nominal=2000_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
            parts=(
                PartDraft(
                    segment_id=segment.id,
                    nominal=2000_00,
                    accrual_days="5",
                    sort_order=1,
                ),
            ),
        ),
        hr,
        sync_external=False,
    )
    issued.certificate.created_at = datetime(2026, 10, 12, 10, 0, tzinfo=MSK)
    db.commit()

    monday = datetime(2026, 10, 19, 4, 0, tzinfo=MSK)
    assert apply_weekly_accruals(db, monday) >= 1
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 2000_00

    thursday = datetime(2026, 10, 22, 23, 59, tzinfo=MSK)
    assert apply_weekly_burns(db, thursday) == 0
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 2000_00

    friday = datetime(2026, 10, 23, 23, 59, tzinfo=MSK)
    assert apply_weekly_burns(db, friday) >= 1
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 0

    saturday = datetime(2026, 10, 24, 12, 0, tzinfo=MSK)
    assert apply_weekly_burns(db, saturday) == 0
    assert issued.certificate.balance == 0

    next_monday = datetime(2026, 10, 26, 4, 0, tzinfo=MSK)
    assert apply_weekly_accruals(db, next_monday) >= 1
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 2000_00


def test_template_rejects_same_or_single_use_combo() -> None:
    db = _db()
    ensure_default_segments(db)
    lunch = next(item for item in list_segments(db) if item.name == "Ланч")
    birthday = next(item for item in list_segments(db) if item.name == "День рождения")
    from bonus.services import BonusError as Err

    with pytest.raises(Err):
        save_template(
            db,
            name="Плохой",
            filename="t.png",
            payload=_tiny_png(),
            content_type="image/png",
            segment_id=lunch.id,
            segment2_id=lunch.id,
        )
    with pytest.raises(Err):
        save_template(
            db,
            name="Разовый комбо",
            filename="t.png",
            payload=_tiny_png(),
            content_type="image/png",
            segment_id=lunch.id,
            segment2_id=birthday.id,
        )


def select_debits(db, certificate_id: int):

    return select(Transaction).where(
        Transaction.certificate_id == certificate_id,
        Transaction.tx_type == TxType.debit,
    )


def _db():
    assert database.SessionLocal is not None
    return database.SessionLocal()


def _person(db, login: str, role: Role) -> User:
    user = User(
        full_name=login,
        login=login,
        password_hash=hash_password("password1"),
        role=role,
        restaurant="",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_weekly_accrual_second_run_same_week_is_noop() -> None:
    """Повторный пн 04:00 в ту же неделю не дублирует credit (регрессия двойного начисления)."""
    db = _db()
    hr = _person(db, "hr", Role.hr)
    segment = create_segment(
        db,
        "Недельный тест",
        AccrualPeriod.weekly,
        accrual_days="5",
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Дубликат тест",
            login="",
            password="",
            nominal=3650_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
        ),
        hr,
        sync_external=False,
    )
    issued.certificate.created_at = datetime(2026, 10, 20, 12, 0, tzinfo=MSK)
    db.commit()

    monday = datetime(2026, 11, 9, 4, 5, tzinfo=MSK)
    assert apply_weekly_accruals(db, monday) == 1
    assert apply_weekly_accruals(db, monday) == 0

    credits = db.scalars(
        select(Transaction).where(
            Transaction.certificate_id == issued.certificate.id,
            Transaction.tx_type == TxType.credit,
            Transaction.comment.like("Автоначисление: еженедельно%"),
        )
    ).all()
    assert len(credits) == 1
    assert credits[0].amount in (0, 3650_00)


def _tiny_png() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (0, 168, 168)).save(buffer, format="PNG")
    return buffer.getvalue()

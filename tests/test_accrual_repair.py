"""Восстановление остатков после отмены дубля автоначисления."""

from datetime import datetime

import bonus.db as database
import pytest
from sqlalchemy import select

from bonus.accrual import apply_weekly_accruals
from bonus.accrual_repair import repair_zero_after_duplicate_undo
from bonus.catalog import create_segment
from bonus.models import AccrualPeriod, Base, Role, Transaction, User
from bonus.security import hash_password
from bonus.parts import change_part_balance, list_parts, sync_certificate_totals
from bonus.services import IssueDraft, credit_certificate, issue_certificate, undo_credit
from bonus.values import MSK


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def test_repair_restores_zero_weekly_part() -> None:
    db = database.open_session()
    hr = User(login="hr", password_hash=hash_password("x"), full_name="HR", role=Role.hr)
    admin = User(login="a", password_hash=hash_password("x"), full_name="Admin", role=Role.admin)
    db.add(hr)
    db.add(admin)
    db.commit()
    segment = create_segment(db, "Неделя", AccrualPeriod.weekly, accrual_days="5")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Иванов Иван",
            login="",
            password="",
            nominal=130_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
        ),
        hr,
        sync_external=False,
    )
    issued.certificate.created_at = datetime(2026, 10, 1, 12, 0, tzinfo=MSK)
    db.commit()
    monday = datetime(2026, 11, 9, 4, 5, tzinfo=MSK)
    apply_weekly_accruals(db, monday)
    marker = "Автоначисление: еженедельно 2026-W46"
    credit_certificate(
        db,
        issued.certificate,
        None,
        130_000,
        marker,
        grow_nominal=False,
    )
    part = list_parts(db, issued.certificate.id)[0]
    change_part_balance(db, part.id, -part.balance)
    sync_certificate_totals(db, issued.certificate.id)
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 0
    result = repair_zero_after_duplicate_undo(db, admin)
    assert result.fixed == 1
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 130_000

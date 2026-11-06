"""Смена лимита автоначисления (номинал части) без разового начисления."""

import bonus.db as database
from bonus.catalog import ensure_default_segments, list_segments
from bonus.models import Base, Role, Transaction, TxType, User
from bonus.parts import list_parts
from bonus.security import hash_password
from bonus.services import (
    BonusError,
    IssueDraft,
    issue_certificate,
    update_certificate_accrual_nominals,
)
import pytest


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def _db():
    return database.open_session()


def _hr(db) -> User:
    user = User(
        full_name="HR Test",
        login="hr",
        password_hash=hash_password("password1"),
        role=Role.hr,
        restaurant="",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_monthly_nominal_change_keeps_balance() -> None:
    db = _db()
    hr = _hr(db)
    ensure_default_segments(db)
    employee = next(item for item in list_segments(db) if item.name == "Карта сотрудника")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Тест",
            login="",
            password="",
            nominal=400_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=employee.id,
        ),
        hr,
    )
    cert = issued.certificate
    parts = list_parts(db, cert.id)
    assert len(parts) == 1
    assert parts[0].nominal == 400_000
    assert parts[0].balance == 400_000

    changes = update_certificate_accrual_nominals(
        db, cert, hr, {parts[0].id: 600_000}
    )
    assert len(changes) == 1
    assert changes[0].new_nominal == 600_000

    db.refresh(parts[0])
    assert parts[0].nominal == 600_000
    assert parts[0].balance == 400_000
    db.refresh(cert)
    assert cert.nominal == 600_000
    assert cert.balance == 400_000

    tx = db.query(Transaction).filter(Transaction.tx_type == TxType.migrate).one()
    assert "600" in tx.comment or "6" in tx.comment


def test_lowering_nominal_caps_balance() -> None:
    db = _db()
    hr = _hr(db)
    ensure_default_segments(db)
    employee = next(item for item in list_segments(db) if item.name == "Карта сотрудника")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Тест 2",
            login="",
            password="",
            nominal=600_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=employee.id,
        ),
        hr,
    )
    part = list_parts(db, issued.certificate.id)[0]
    update_certificate_accrual_nominals(db, issued.certificate, hr, {part.id: 300_000})
    db.refresh(part)
    assert part.nominal == 300_000
    assert part.balance == 300_000


def test_no_change_raises() -> None:
    db = _db()
    hr = _hr(db)
    ensure_default_segments(db)
    employee = next(item for item in list_segments(db) if item.name == "Карта сотрудника")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Тест 3",
            login="",
            password="",
            nominal=400_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=employee.id,
        ),
        hr,
    )
    part = list_parts(db, issued.certificate.id)[0]
    with pytest.raises(BonusError):
        update_certificate_accrual_nominals(db, issued.certificate, hr, {part.id: 400_000})

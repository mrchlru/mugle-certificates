"""Списание iiko должно уменьшать части карты, иначе баланс на карточке не меняется."""

from __future__ import annotations

import bonus.db as database
from bonus.catalog import ensure_default_segments, list_segments
from bonus.models import AccrualPeriod, Base, Role, User
from bonus.parts import PartDraft, list_parts
from bonus.present import load_card
from bonus.security import hash_password
from bonus.services import (
    IssueDraft,
    debit_for_iiko,
    issue_certificate,
    refund_for_iiko,
    repair_iiko_part_balance_drift,
)
from bonus.values import MSK
from datetime import datetime


def setup_function() -> None:
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def test_iiko_debit_reduces_parts_and_card_balance() -> None:
    db = database.SessionLocal()
    hr = _person(db, "hr", Role.hr)
    ensure_default_segments(db)
    monthly = next(
        item for item in list_segments(db) if item.name == "Ежемесячные бонусы"
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Касса Тест",
            login="",
            password="",
            nominal=10_000_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=monthly.id,
            parts=(
                PartDraft(
                    segment_id=monthly.id,
                    nominal=10_000_00,
                    use_days="1234567",
                    accrual_days="12345",
                    sort_order=1,
                ),
            ),
        ),
        hr,
        sync_external=False,
    )
    cert = issued.certificate
    result = debit_for_iiko(
        db,
        cert,
        amount=2_990_00,
        external_id="tx-59190",
        restaurant="Базар",
        order_ref="59190",
        cheque_detail="Ресторан: Базар\nЗаказ: №59190",
    )
    assert result.paid == 2_990_00
    assert result.balance_after == 7_010_00
    db.refresh(cert)
    parts = list_parts(db, cert.id)
    assert sum(part.balance for part in parts) == 7_010_00
    card = load_card(db, cert, "http://test", hr)
    assert card.balance == 7_010_00
    assert any(item.cheque_detail.startswith("Ресторан:") for item in card.history)


def test_repair_aligns_parts_after_legacy_cert_only_debit() -> None:
    db = database.SessionLocal()
    hr = _person(db, "hr", Role.hr)
    ensure_default_segments(db)
    monthly = next(
        item for item in list_segments(db) if item.name == "Ежемесячные бонусы"
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Дрейф",
            login="",
            password="",
            nominal=36_359_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=monthly.id,
            parts=(
                PartDraft(
                    segment_id=monthly.id,
                    nominal=36_359_00,
                    use_days="1234567",
                    accrual_days="12345",
                    sort_order=1,
                ),
            ),
        ),
        hr,
        sync_external=False,
    )
    cert = issued.certificate
    # Имитация бага: списали только certificate.balance.
    cert.balance = 36_359_00 - 2_990_00
    db.commit()
    db.refresh(cert)
    assert sum(part.balance for part in list_parts(db, cert.id)) == 36_359_00
    assert repair_iiko_part_balance_drift(db, cert) is True
    db.commit()
    db.refresh(cert)
    assert cert.balance == 33_369_00
    assert sum(part.balance for part in list_parts(db, cert.id)) == 33_369_00


def test_iiko_refund_restores_part() -> None:
    db = database.SessionLocal()
    hr = _person(db, "hr", Role.hr)
    ensure_default_segments(db)
    monthly = next(
        item for item in list_segments(db) if item.accrual_period == AccrualPeriod.monthly
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Возврат",
            login="",
            password="",
            nominal=5_000_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=monthly.id,
            parts=(
                PartDraft(
                    segment_id=monthly.id,
                    nominal=5_000_00,
                    use_days="1234567",
                    accrual_days="12345",
                    sort_order=1,
                ),
            ),
        ),
        hr,
        sync_external=False,
    )
    debit_for_iiko(
        db,
        issued.certificate,
        amount=1_000_00,
        external_id="refund-1",
        restaurant="Тест",
    )
    refund_for_iiko(db, "refund-1", "отмена")
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 5_000_00
    assert sum(part.balance for part in list_parts(db, issued.certificate.id)) == 5_000_00


def _person(db, login: str, role: Role) -> User:
    user = User(
        login=login,
        full_name=login,
        role=role,
        password_hash=hash_password("secret"),
        restaurant="",
        created_at=datetime.now(MSK),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

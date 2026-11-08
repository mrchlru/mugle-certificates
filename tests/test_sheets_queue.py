"""Очередь Google Sheets: выпуск и обновления не бьют API параллельно."""

from unittest.mock import patch

import pytest

import bonus.db as database
from bonus.catalog import create_segment
from bonus.models import AccrualPeriod, Base, Role, SheetsSyncJob
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate
from bonus.sheets_queue import enqueue_certificate_ids, process_sheets_queue_once
from sqlalchemy import select


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def _db():
    return database.SessionLocal()


def _person(db, login: str, role: Role):
    from bonus.models import User

    user = User(
        login=login,
        password_hash=hash_password("x" * 12),
        full_name="Тест",
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_sync_usage_only_enqueues_not_direct_sync() -> None:
    """Списание ставит задачу в очередь, без прямого вызова sync_certificate_usage."""
    db = _db()
    hr = _person(db, "hr", Role.hr)
    segment = create_segment(db, "Тест", AccrualPeriod.none)
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Иван",
            login="",
            password="",
            nominal=1_000_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
        ),
        hr,
        sync_external=True,
    )
    with patch("bonus.sheets_queue.enqueue_certificate_ids", wraps=enqueue_certificate_ids) as enq:
        with patch("bonus.sheets_sync.sync_certificate_usage") as direct:
            from bonus.services import debit_certificate

            manager = _person(db, "mgr", Role.manager)
            debit_certificate(db, issued.certificate, manager, 100_00, "тест")
    assert enq.call_count >= 1
    direct.assert_not_called()
    job = db.scalar(
        select(SheetsSyncJob).where(SheetsSyncJob.certificate_id == issued.certificate.id)
    )
    assert job is not None
    assert job.status == "pending"
    assert job.job_kind == "sync"


def test_process_queue_calls_sync_once_per_job() -> None:
    db = _db()
    hr = _person(db, "hr2", Role.hr)
    segment = create_segment(db, "Т2", AccrualPeriod.none)
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Пётр",
            login="",
            password="",
            nominal=500_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
        ),
        hr,
        sync_external=False,
    )
    enqueue_certificate_ids(db, [issued.certificate.id])
    with patch("bonus.sheets_queue.sheets_enabled", return_value=True):
        with patch(
            "bonus.sheets_queue.sync_certificate_usage",
            return_value=True,
        ) as sync:
            stats = process_sheets_queue_once(limit=10)
    assert stats["written"] == 1
    sync.assert_called_once()
    job = db.scalar(
        select(SheetsSyncJob).where(SheetsSyncJob.certificate_id == issued.certificate.id)
    )
    assert job is not None
    assert job.status == "done"

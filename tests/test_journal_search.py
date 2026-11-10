"""Поиск в журнале операций и контроле."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.audit import list_audit, normalize_audit_action_filter, write_audit
from bonus.catalog import ensure_default_segments
from bonus.models import AuditAction, Base, Role, TxType, User
from bonus.present import ledger, normalize_ledger_tx_filter
from bonus.security import hash_password
from bonus.services import IssueDraft, debit_certificate, issue_certificate


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    """Пустая in-memory база на каждый тест."""
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def _db() -> Session:
    return database.open_session()


def _person(db: Session, login: str, role: Role) -> User:
    user = User(
        login=login,
        password_hash=hash_password("x"),
        full_name=f"User {login}",
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_ledger_search_by_holder() -> None:
    """Журнал находит операции по имени держателя."""
    db = _db()
    ensure_default_segments(db)
    actor = _person(db, "admin", Role.admin)
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Иванов Иван",
            login="holder1",
            password="",
            nominal=50_000,
            balance=None,
            note="",
            expires_on=None,
        ),
        actor=actor,
    )
    certificate = issued.certificate
    actor.restaurant = "Марико"
    db.commit()
    debit_certificate(db, certificate, actor, 1_000, "Обед", enforce_weekdays=False)
    rows = ledger(db, actor, query="иванов", tx_filter="all")
    assert rows
    assert any("Иванов" in item.holder_name for item in rows)


def test_ledger_filter_debit_only() -> None:
    """Фильтр оставляет только списания."""
    db = _db()
    ensure_default_segments(db)
    actor = _person(db, "admin", Role.admin)
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Петров Петр",
            login="holder2",
            password="",
            nominal=10_000,
            balance=None,
            note="",
            expires_on=None,
        ),
        actor=actor,
    )
    debit_certificate(db, issued.certificate, actor, 500, "Кофе", enforce_weekdays=False)
    rows = ledger(db, actor, tx_filter="debit")
    assert rows
    assert all(item.tx_type == TxType.debit.value for item in rows)


def test_normalize_ledger_tx_filter() -> None:
    """Неизвестный фильтр журнала сбрасывается в all."""
    assert normalize_ledger_tx_filter("credit") == "credit"
    assert normalize_ledger_tx_filter("nope") == "all"


def test_audit_search_summary() -> None:
    """Контроль ищет по тексту сводки."""
    db = _db()
    actor = _person(db, "admin", Role.admin)
    write_audit(
        db,
        actor=actor,
        action=AuditAction.certificate_issue,
        summary="Тестовый выпуск для поиска",
    )
    rows = list_audit(db, query="тестовый выпуск")
    assert rows
    assert rows[0].summary.startswith("Тестовый")


def test_normalize_audit_action_filter() -> None:
    """Фильтр контроля принимает только известные действия."""
    assert normalize_audit_action_filter("certificate_issue") == "certificate_issue"
    assert normalize_audit_action_filter("unknown") == "all"

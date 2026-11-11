"""Блокировка списания бонусов с отдельной кассы."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.catalog import ensure_default_segments, list_segments
from bonus.config import get_settings
from bonus.iiko_monitor import set_terminal_debit_blocked, upsert_heartbeat
from bonus.main import create_app
from bonus.models import Base, Certificate, IikoTerminal, Role, User
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SEED_DEMO", "false")
    monkeypatch.setenv("IIKO_API_KEY", "test-iiko-key")
    get_settings.cache_clear()
    database.configure_database("sqlite:///:memory:")
    Base.metadata.create_all(database.engine)
    with TestClient(create_app()) as test_client:
        db = database.open_session()
        try:
            ensure_default_segments(db)
            _admin(db)
            _card(db)
        finally:
            db.close()
        yield test_client
    get_settings.cache_clear()


def _admin(db: Session) -> None:
    db.add(
        User(
            full_name="Admin",
            login="admin-test",
            password_hash=hash_password("secret-long"),
            role=Role.admin,
        )
    )
    db.commit()


def _card(db: Session) -> Certificate:
    segment = next(s for s in list_segments(db) if s.name == "Карта сотрудника")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Касса Тест",
            login="",
            password="",
            nominal=100_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
            segment_name=segment.name,
            company="ООО",
            ledger_sheet="employee",
        ),
        None,
        sync_external=False,
    )
    return issued.certificate


def _seed_terminal(db: Session) -> tuple[str, int]:
    terminal_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    upsert_heartbeat(
        db,
        {
            "terminal_id": terminal_id,
            "restaurant": "Coffee Cake НН Аэропорт",
            "machine_name": "KASSA-AERO-NN",
            "plugin_version": "1.1.0",
            "features": ["debit-code"],
        },
        "127.0.0.1",
    )
    row = db.scalar(select(IikoTerminal).where(IikoTerminal.terminal_id == terminal_id))
    assert row is not None
    return terminal_id, row.id


def test_debit_rejected_by_restaurant_without_terminal_id(client: TestClient) -> None:
    """Плагин 1.1.0 не шлёт terminal_id — только restaurant в теле debit."""
    db = database.open_session()
    try:
        _terminal_id, db_id = _seed_terminal(db)
        cert = db.scalar(select(Certificate).limit(1))
        assert cert is not None
        set_terminal_debit_blocked(db, db_id, True)
        code = cert.short_code
    finally:
        db.close()

    response = client.post(
        "/api/iiko/debit",
        headers={"X-Iiko-Key": "test-iiko-key"},
        json={
            "code": code,
            "amount_rub": "100",
            "transaction_id": "22222222-3333-4444-5555-666666666666",
            "restaurant": "Coffee Cake НН Аэропорт",
        },
    )
    assert response.status_code == 403


def test_debit_rejected_when_terminal_blocked(client: TestClient) -> None:
    db = database.open_session()
    try:
        terminal_id, db_id = _seed_terminal(db)
        cert = db.scalar(select(Certificate).limit(1))
        assert cert is not None
        code = cert.short_code
        set_terminal_debit_blocked(db, db_id, True)
    finally:
        db.close()

    response = client.post(
        "/api/iiko/debit",
        headers={"X-Iiko-Key": "test-iiko-key", "X-Terminal-Id": terminal_id},
        json={
            "code": code,
            "amount_rub": "100",
            "transaction_id": "11111111-2222-3333-4444-555555555555",
            "restaurant": "Coffee Cake НН Аэропорт",
        },
    )
    assert response.status_code == 403


def test_kassy_toggle_block(client: TestClient) -> None:
    db = database.open_session()
    try:
        _terminal_id, terminal_db_id = _seed_terminal(db)
    finally:
        db.close()

    login_page = client.get("/login")
    csrf_login = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert csrf_login is not None
    client.post(
        "/login",
        data={
            "login": "admin-test",
            "password": "secret-long",
            "csrf_token": csrf_login.group(1),
        },
        follow_redirects=False,
    )
    page = client.get("/hr/kassy")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None

    blocked = client.post(
        f"/hr/kassy/terminals/{terminal_db_id}/debit-block",
        data={"csrf_token": match.group(1), "blocked": "1"},
        follow_redirects=False,
    )
    assert blocked.status_code == 303

    db = database.open_session()
    try:
        row = db.get(IikoTerminal, terminal_db_id)
        assert row is not None
        assert row.debit_blocked is True
    finally:
        db.close()

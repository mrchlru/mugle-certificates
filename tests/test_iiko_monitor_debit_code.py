"""Heartbeat касс и подтверждение списания кодом."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.app_settings import get_app_settings, update_debit_code_settings
from bonus.catalog import ensure_default_segments, list_segments
from bonus.config import get_settings
from bonus.iiko_monitor import build_dashboard, upsert_heartbeat
from bonus.main import create_app
from bonus.models import Base, IikoTerminal, Role, User
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SEED_DEMO", "false")
    monkeypatch.setenv("IIKO_API_KEY", "test-iiko-key")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        db = database.open_session()
        try:
            ensure_default_segments(db)
            _admin(db)
            _linked_holder(db, spasibo_id="42", require_code=False)
        finally:
            db.close()
        yield test_client
    get_settings.cache_clear()


def _admin(db: Session) -> User:
    user = User(
        full_name="Admin",
        login="admin-test",
        password_hash=hash_password("secret-long"),
        role=Role.admin,
    )
    db.add(user)
    db.commit()
    return user


def _linked_holder(
    db: Session,
    *,
    spasibo_id: str,
    require_code: bool,
    balance: int = 500_000,
) -> tuple[User, object]:
    employee = next(s for s in list_segments(db) if s.name == "Карта сотрудника")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Касса Тест",
            login="",
            password="",
            nominal=balance,
            balance=None,
            note="",
            expires_on=None,
            segment_id=employee.id,
            segment_name=employee.name,
            company="ООО",
            ledger_sheet="employee",
        ),
        None,
        sync_external=False,
    )
    issued.user.spasibo_user_id = spasibo_id
    issued.user.spasibo_certificate_id = issued.certificate.id
    issued.certificate.require_debit_code = require_code
    db.commit()
    return issued.user, issued.certificate


def _card_code(db: Session) -> str:
    from bonus.models import Certificate

    cert = db.scalars(db.query(Certificate).limit(1)).first()  # type: ignore[attr-defined]
    cert = db.query(Certificate).first()
    return cert.short_code


def test_heartbeat_upsert_registers_terminal(client: TestClient) -> None:
    db = database.open_session()
    try:
        upsert_heartbeat(
            db,
            {
                "terminal_id": "term-1",
                "restaurant": "Ресторан Тест",
                "machine_name": "KASSA-1",
                "plugin_version": "1.1.0",
                "license_ok": True,
                "payment_type_bound": True,
                "features": ["heartbeat", "debit-code"],
            },
            "127.0.0.1",
        )
        dash = build_dashboard(db)
        assert dash["counts"]["total"] == 1
        assert dash["rows"][0]["restaurant"] == "Ресторан Тест"
        assert dash["rows"][0]["status_key"] == "ok"
    finally:
        db.close()


def test_debit_disabled_by_default(client: TestClient) -> None:
    db = database.open_session()
    try:
        code = db.query(__import__("bonus.models", fromlist=["Certificate"]).Certificate).first().short_code
    finally:
        db.close()
    response = client.post(
        "/api/iiko/debit",
        headers={
            "X-Iiko-Key": "test-iiko-key",
            "X-Plugin-Features": "debit-code",
        },
        json={
            "code": code,
            "amount_rub": "5000",
            "transaction_id": "tx-default-1",
        },
    )
    assert response.status_code == 200


def test_debit_code_required_for_marked_card(client: TestClient) -> None:
    db = database.open_session()
    try:
        update_debit_code_settings(
            db,
            enabled=True,
            scope="marked",
            min_amount=100,
            ttl_sec=120,
            max_attempts=3,
            on_failure="deny",
        )
        cert = db.query(__import__("bonus.models", fromlist=["Certificate"]).Certificate).first()
        cert.require_debit_code = True
        db.commit()
        code = cert.short_code
    finally:
        db.close()

    response = client.post(
        "/api/iiko/debit",
        headers={"X-Iiko-Key": "test-iiko-key", "X-Plugin-Features": "debit-code"},
        json={"code": code, "amount_rub": "5000", "transaction_id": "tx-code-1"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "code_required"
    assert "challenge_id" in body


def test_old_plugin_rejected_when_code_required(client: TestClient) -> None:
    db = database.open_session()
    try:
        update_debit_code_settings(
            db,
            enabled=True,
            scope="all",
            min_amount=0,
            ttl_sec=120,
            max_attempts=3,
            on_failure="deny",
        )
        code = db.query(__import__("bonus.models", fromlist=["Certificate"]).Certificate).first().short_code
    finally:
        db.close()
    response = client.post(
        "/api/iiko/debit",
        headers={"X-Iiko-Key": "test-iiko-key"},
        json={"code": code, "amount_rub": "100", "transaction_id": "tx-old-1"},
    )
    assert response.status_code == 400
    assert "Обновите плагин" in response.json()["detail"]

"""API /api/spasibo для приложения «Спасибо»."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.catalog import ensure_default_segments, list_segments
from bonus.config import get_settings
from bonus.main import create_app
from bonus.models import Base, Role, User
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SEED_DEMO", "false")
    monkeypatch.setenv("SPASIBO_API_KEY", "test-spasibo-key")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        db = database.open_session()
        try:
            ensure_default_segments(db)
            user, cert = _linked_holder(db, spasibo_id="321")
            db.refresh(user)
            db.refresh(cert)
        finally:
            db.close()
        yield test_client
    get_settings.cache_clear()


def _linked_holder(db: Session, spasibo_id: str) -> tuple[User, object]:
    employee = next(s for s in list_segments(db) if s.name == "Карта сотрудника")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="API Тест",
            login="",
            password="",
            nominal=5_000,
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
    db.commit()
    return issued.user, issued.certificate


def test_spasibo_card_requires_key(client: TestClient) -> None:
    response = client.get("/api/spasibo/card?user_id=321")
    assert response.status_code == 401


def test_spasibo_card_linked(client: TestClient) -> None:
    response = client.get(
        "/api/spasibo/card?user_id=321",
        headers={"X-Spasibo-Key": "test-spasibo-key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["linked"] is True
    assert data["balance_kopecks"] == 5_000


def test_spasibo_link_by_public_number(client: TestClient) -> None:
    db = database.open_session()
    try:
        _, cert = _linked_holder(db, spasibo_id="999")
        cert.holder.spasibo_user_id = ""
        cert.holder.spasibo_certificate_id = None
        db.commit()
        public_number = cert.public_number
    finally:
        db.close()
    response = client.post(
        "/api/spasibo/link",
        json={
            "spasibo_user_id": "42",
            "teams_card": public_number,
            "replace_conflicts": True,
        },
        headers={"X-Spasibo-Key": "test-spasibo-key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["linked"] is True
    assert data["public_number"] == public_number
    card = client.get(
        "/api/spasibo/card?user_id=42",
        headers={"X-Spasibo-Key": "test-spasibo-key"},
    )
    assert card.status_code == 200
    assert card.json()["linked"] is True


def test_spasibo_redeem_idempotent(client: TestClient) -> None:
    headers = {
        "X-Spasibo-Key": "test-spasibo-key",
        "Idempotency-Key": "purchase-1",
    }
    first = client.post(
        "/api/spasibo/redeem",
        json={"spasibo_user_id": "321", "bonus_amount_rub": 100},
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["replayed"] is False
    second = client.post(
        "/api/spasibo/redeem",
        json={"spasibo_user_id": "321", "bonus_amount_rub": 100},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["replayed"] is True

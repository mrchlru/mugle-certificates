"""Выгрузка всех карт и доступ к рассылке."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.cards_export import build_all_cards_workbook
from bonus.catalog import ensure_default_segments, list_segments
from bonus.models import Base, Role, User
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SEED_DEMO", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret-cards-export")
    from bonus.config import get_settings
    from bonus.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        db = database.open_session()
        try:
            ensure_default_segments(db)
            _admin(db)
            hr = _hr(db)
            segment = list_segments(db)[0]
            issue_certificate(
                db,
                IssueDraft(
                    full_name="Иван Тестов",
                    login="",
                    password="",
                    nominal=100_000,
                    balance=None,
                    note="тест",
                    expires_on=None,
                    segment_id=segment.id,
                    segment_name=segment.name,
                    company="ООО Тест",
                    ledger_sheet="employee",
                ),
                hr,
                sync_external=False,
            )
        finally:
            db.close()
        yield test_client
    get_settings.cache_clear()


def _admin(db: Session) -> User:
    user = User(
        full_name="Admin",
        login="admin-cards",
        password_hash=hash_password("secret-long"),
        role=Role.admin,
    )
    db.add(user)
    db.commit()
    return user


def _hr(db: Session) -> User:
    user = User(
        full_name="HR",
        login="hr-cards",
        password_hash=hash_password("secret-long"),
        role=Role.hr,
    )
    db.add(user)
    db.commit()
    return user


def test_build_all_cards_workbook_has_link(client: TestClient) -> None:
    db = database.open_session()
    try:
        payload = build_all_cards_workbook(db, base_url="https://example.test")
    finally:
        db.close()
    book = load_workbook(io.BytesIO(payload), read_only=True)
    sheet = book.active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][0] == "ID карты"
    assert rows[1][2]  # код карты
    assert "https://example.test/c/" in str(rows[1][-1])


def test_cards_export_admin_only(client: TestClient) -> None:
    page = client.get("/login")
    token = _csrf(page.text)
    client.post(
        "/login",
        data={"login": "hr-cards", "password": "secret-long", "csrf_token": token},
        follow_redirects=True,
    )
    denied = client.get("/hr/settings/cards-export.xlsx", follow_redirects=False)
    assert denied.status_code in {302, 303}

    client.post("/logout", data={"csrf_token": _csrf(client.get("/login").text)})
    page = client.get("/login")
    token = _csrf(page.text)
    client.post(
        "/login",
        data={"login": "admin-cards", "password": "secret-long", "csrf_token": token},
        follow_redirects=True,
    )
    ok = client.get("/hr/settings/cards-export.xlsx")
    assert ok.status_code == 200
    assert "spreadsheetml" in ok.headers.get("content-type", "")


def _csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]

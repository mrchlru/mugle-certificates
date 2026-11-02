"""Блок входа по IP, «Забыли пароль?» и разблокировка admin."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bonus.db as database
from bonus.config import get_settings
from bonus.login_guard import (
    MAX_FAILS,
    login_block_status,
    normalize_support_telegram,
    register_login_failure,
    register_login_success,
)
from bonus.models import AuditAction, AuditLog, Base, LoginAttempt, Role, User
from bonus.security import hash_password
from sqlalchemy import select


@pytest.fixture()
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'login.db'}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-login-guard")
    monkeypatch.setenv("SEED_DEMO", "true")
    monkeypatch.setenv("SUPPORT_TELEGRAM", "@nkatine")
    monkeypatch.setenv("LOGIN_ALLOW_IPS", "")
    get_settings.cache_clear()
    database.configure_database(f"sqlite:///{tmp_path / 'login.db'}")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    from bonus.schema import ensure_schema

    ensure_schema(database.engine)
    import bonus.main as main

    with TestClient(main.app) as client:
        yield client, tmp_path
    get_settings.cache_clear()


def test_five_fails_block_even_correct_password(app_client, monkeypatch) -> None:
    client, _ = app_client
    monkeypatch.setattr("bonus.routes_auth.client_ip", lambda _request: "10.0.0.1")
    csrf = _csrf(client.get("/login").text)
    for _ in range(MAX_FAILS):
        response = client.post(
            "/login",
            data={
                "login": "hr",
                "password": "wrong",
                "csrf_token": csrf,
                "next": "/",
            },
        )
        assert response.status_code in {400, 429}
        csrf = _csrf(response.text)
    blocked = client.post(
        "/login",
        data={
            "login": "hr",
            "password": "mugle-hr",
            "csrf_token": csrf,
            "next": "/",
        },
    )
    assert blocked.status_code == 429
    assert "закрыт до" in blocked.text
    assert "МСК" in blocked.text


def test_block_expires_after_six_hours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_db(tmp_path, monkeypatch)
    db = database.SessionLocal()
    assert db is not None
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    for _ in range(MAX_FAILS):
        register_login_failure(db, "10.1.1.1", login="hr", now=now)
    blocked, until = login_block_status(db, "10.1.1.1", now=now)
    assert blocked is True
    assert until is not None
    later = now + timedelta(hours=6, minutes=1)
    blocked2, _ = login_block_status(db, "10.1.1.1", now=later)
    assert blocked2 is False
    register_login_failure(db, "10.1.1.1", login="hr", now=later)
    row = db.get(LoginAttempt, "10.1.1.1")
    assert row is not None
    assert row.fail_count == 1
    db.close()


def test_success_resets_counter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_db(tmp_path, monkeypatch)
    db = database.SessionLocal()
    assert db is not None
    now = datetime.now(timezone.utc)
    for _ in range(4):
        register_login_failure(db, "10.2.2.2", login="hr", now=now)
    register_login_success(db, "10.2.2.2")
    assert db.get(LoginAttempt, "10.2.2.2") is None
    for _ in range(4):
        register_login_failure(db, "10.2.2.2", login="hr", now=now)
    blocked, _ = login_block_status(db, "10.2.2.2", now=now)
    assert blocked is False
    db.close()


def test_block_survives_reconfigure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "persist.db"
    url = f"sqlite:///{db_path}"
    _prepare_db(tmp_path, monkeypatch, url=url)
    db = database.SessionLocal()
    assert db is not None
    now = datetime.now(timezone.utc)
    for _ in range(MAX_FAILS):
        register_login_failure(db, "10.3.3.3", login="x", now=now)
    db.close()
    database.configure_database(url)
    db2 = database.SessionLocal()
    assert db2 is not None
    blocked, _ = login_block_status(db2, "10.3.3.3", now=now)
    assert blocked is True
    db2.close()


def test_other_ip_not_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_db(tmp_path, monkeypatch)
    db = database.SessionLocal()
    assert db is not None
    now = datetime.now(timezone.utc)
    for _ in range(MAX_FAILS):
        register_login_failure(db, "10.4.4.4", login="hr", now=now)
    blocked, _ = login_block_status(db, "10.5.5.5", now=now)
    assert blocked is False
    db.close()


def test_x_forwarded_for_ignored(app_client, monkeypatch) -> None:
    client, _ = app_client
    seen: list[str] = []

    def _capture(request):
        from bonus.security import client_ip as real

        ip = real(request)
        seen.append(ip)
        return ip

    monkeypatch.setattr("bonus.routes_auth.client_ip", _capture)
    csrf = _csrf(client.get("/login").text)
    client.post(
        "/login",
        data={"login": "hr", "password": "bad", "csrf_token": csrf, "next": "/"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert seen
    assert seen[0] != "203.0.113.9"


def test_unknown_login_same_message(app_client, monkeypatch) -> None:
    client, _ = app_client
    monkeypatch.setattr("bonus.routes_auth.client_ip", lambda _r: "10.6.6.6")
    csrf = _csrf(client.get("/login").text)
    missing = client.post(
        "/login",
        data={
            "login": "no-such-user",
            "password": "x",
            "csrf_token": csrf,
            "next": "/",
        },
    )
    csrf2 = _csrf(missing.text)
    wrong = client.post(
        "/login",
        data={
            "login": "hr",
            "password": "bad-password",
            "csrf_token": csrf2,
            "next": "/",
        },
    )
    assert "Неверный логин или пароль" in missing.text
    assert "Неверный логин или пароль" in wrong.text
    assert "Осталось попыток" in missing.text
    assert "Осталось попыток" in wrong.text


def test_csrf_and_employee_do_not_count(app_client, monkeypatch) -> None:
    client, _ = app_client
    monkeypatch.setattr("bonus.routes_auth.client_ip", lambda _r: "10.7.7.7")
    db = database.SessionLocal()
    assert db is not None
    employee = User(
        full_name="Сотрудник",
        login="emp-guard",
        password_hash=hash_password("secret123"),
        role=Role.employee,
        restaurant="",
        is_active=True,
    )
    db.add(employee)
    db.commit()
    db.close()

    bad_csrf = client.post(
        "/login",
        data={
            "login": "hr",
            "password": "mugle-hr",
            "csrf_token": "not-a-token",
            "next": "/",
        },
    )
    assert bad_csrf.status_code == 400
    assert "Сессия устарела" in bad_csrf.text

    csrf = _csrf(client.get("/login").text)
    employee_resp = client.post(
        "/login",
        data={
            "login": "emp-guard",
            "password": "secret123",
            "csrf_token": csrf,
            "next": "/",
        },
    )
    assert "ссылке" in employee_resp.text.casefold()

    db = database.SessionLocal()
    assert db is not None
    assert db.get(LoginAttempt, "10.7.7.7") is None
    db.close()


def test_forgot_password_support_handle(app_client, monkeypatch) -> None:
    client, _ = app_client
    page = client.get("/login")
    assert "Забыли пароль" in page.text
    assert "@nkatine" in page.text
    assert 'href="https://t.me/nkatine"' in page.text
    forgot = client.get("/forgot")
    assert forgot.status_code == 200
    assert "@nkatine" in forgot.text

    monkeypatch.setenv("SUPPORT_TELEGRAM", "")
    get_settings.cache_clear()
    empty = client.get("/login")
    assert "Забыли пароль" not in empty.text

    assert normalize_support_telegram("<script>") == ""
    assert normalize_support_telegram("@ok_user") == "@ok_user"


def test_admin_unblock_writes_audit(app_client, monkeypatch) -> None:
    client, _ = app_client
    monkeypatch.setattr("bonus.routes_auth.client_ip", lambda _r: "10.8.8.8")
    csrf = _csrf(client.get("/login").text)
    for _ in range(MAX_FAILS):
        response = client.post(
            "/login",
            data={
                "login": "hr",
                "password": "wrong",
                "csrf_token": csrf,
                "next": "/",
            },
        )
        csrf = _csrf(response.text)

    # Admin заходит с другого IP — иначе общий блок 10.8.8.8 мешает.
    monkeypatch.setattr("bonus.routes_auth.client_ip", lambda _r: "10.9.9.9")
    csrf = _csrf(client.get("/login").text)
    admin = client.post(
        "/login",
        data={
            "login": "admin",
            "password": "teams-admin",
            "csrf_token": csrf,
            "next": "/hr/settings",
        },
        follow_redirects=False,
    )
    assert admin.status_code in {303, 302}

    settings = client.get("/hr/settings")
    assert settings.status_code == 200
    assert "10.8.8.8" in settings.text
    csrf = _csrf(settings.text)
    unblock = client.post(
        "/hr/settings/login-unblock",
        data={"ip": "10.8.8.8", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert unblock.status_code == 303

    db = database.SessionLocal()
    assert db is not None
    assert db.get(LoginAttempt, "10.8.8.8") is None
    audit = db.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.login_unblock.value)
    )
    assert audit is not None
    assert "10.8.8.8" in (audit.detail or "")
    db.close()

    monkeypatch.setattr("bonus.routes_auth.client_ip", lambda _r: "10.8.8.8")
    csrf = _csrf(client.get("/login").text)
    again = client.post(
        "/login",
        data={
            "login": "hr",
            "password": "mugle-hr",
            "csrf_token": csrf,
            "next": "/",
        },
        follow_redirects=False,
    )
    assert again.status_code == 303


def _prepare_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str | None = None,
) -> None:
    db_url = url or f"sqlite:///{tmp_path / 'guard.db'}"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("SUPPORT_TELEGRAM", "@nkatine")
    get_settings.cache_clear()
    database.configure_database(db_url)
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)

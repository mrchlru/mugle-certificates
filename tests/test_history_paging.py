"""Ленивая подгрузка истории операций по карточке."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

import bonus.db as database
from bonus.config import get_settings
from bonus.models import (
    Base,
    Certificate,
    Role,
    Transaction,
    TxType,
    User,
)
from bonus.present import HISTORY_PAGE_SIZE, load_card, load_history_page
from bonus.services import IssueDraft, issue_certificate, set_blocked, set_certificate_expiry
from bonus.values import today_msk


@pytest.fixture(autouse=True)
def _fast_password(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_hash(raw: str) -> str:
        return f"hash:{raw}"

    def fake_verify(raw: str, stored: str) -> bool:
        return stored == f"hash:{raw}"

    monkeypatch.setattr("bonus.security.hash_password", fake_hash)
    monkeypatch.setattr("bonus.security.verify_password", fake_verify)
    # bootstrap/services держат имя hash_password с момента import
    monkeypatch.setattr("bonus.bootstrap.hash_password", fake_hash)


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-history-paging")
    monkeypatch.setenv("SEED_DEMO", "false")
    get_settings.cache_clear()
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    from bonus.schema import ensure_schema

    ensure_schema(database.engine)
    yield
    get_settings.cache_clear()


@pytest.fixture()
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_db):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'history.db'}")
    get_settings.cache_clear()
    database.configure_database(f"sqlite:///{tmp_path / 'history.db'}")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    from bonus.schema import ensure_schema

    ensure_schema(database.engine)
    import bonus.main as main

    with TestClient(main.app) as client:
        yield client
    get_settings.cache_clear()


def test_history_pages_cover_all_without_dupes() -> None:
    db = _db()
    hr, certificate = _issued(db)
    _replace_transactions(db, certificate, hr, 35)
    first = load_history_page(db, certificate, hr, limit=HISTORY_PAGE_SIZE)
    assert len(first.items) == 10
    assert first.has_more is True
    assert first.next_before == first.items[-1].id

    collected = list(first.items)
    cursor = first.next_before
    sizes = []
    for _ in range(3):
        page = load_history_page(
            db,
            certificate,
            hr,
            limit=HISTORY_PAGE_SIZE,
            before_id=cursor,
        )
        sizes.append(len(page.items))
        collected.extend(page.items)
        cursor = page.next_before
        if not page.has_more:
            break
    assert sizes == [10, 10, 5]
    assert len(collected) == 35
    ids = [item.id for item in collected]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids, reverse=True)
    assert cursor == collected[-1].id
    assert load_history_page(
        db, certificate, hr, limit=HISTORY_PAGE_SIZE, before_id=cursor
    ).has_more is False


def test_undone_flag_across_pages() -> None:
    db = _db()
    hr, certificate = _issued(db)
    # 9 старых операций + debit + filler + reversal сверху → debit и reversal на разных страницах
    _add_transactions(db, certificate, hr, 9)
    debit = Transaction(
        certificate_id=certificate.id,
        actor_id=hr.id,
        tx_type=TxType.debit,
        amount=100,
        balance_after=0,
        comment="debit-cross",
    )
    db.add(debit)
    db.commit()
    db.refresh(debit)
    _add_transactions(db, certificate, hr, 10, prefix="mid")
    reversal = Transaction(
        certificate_id=certificate.id,
        actor_id=hr.id,
        tx_type=TxType.credit,
        amount=100,
        balance_after=100,
        comment="undo",
        reversal_of_id=debit.id,
    )
    db.add(reversal)
    db.commit()

    first = load_history_page(db, certificate, hr, limit=10)
    assert all(item.id != debit.id for item in first.items)
    assert any(item.reversal_of_id == debit.id for item in first.items)

    second = load_history_page(
        db,
        certificate,
        hr,
        limit=10,
        before_id=first.next_before,
    )
    debit_view = next(item for item in second.items if item.id == debit.id)
    assert debit_view.undone is True


def test_history_endpoint_guards(app_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client = app_client
    db = _db()
    hr, certificate = _issued(db)
    _add_transactions(db, certificate, hr, 12)
    token = certificate.token

    assert client.get(f"/c/unknown-token/history?before=1").status_code == 404

    deleted = issue_certificate(db, _draft(50_000), hr, sync_external=False).certificate
    deleted.is_deleted = True
    db.commit()
    assert client.get(f"/c/{deleted.token}/history?before=1").status_code == 404

    blocked = issue_certificate(db, _draft(50_000), hr, sync_external=False).certificate
    set_blocked(db, blocked, hr, True)
    assert client.get(f"/c/{blocked.token}/history?before=1").status_code == 404

    expired = issue_certificate(db, _draft(50_000), hr, sync_external=False).certificate
    set_certificate_expiry(db, expired, today_msk() - timedelta(days=1), hr)
    assert client.get(f"/c/{expired.token}/history?before=1").status_code == 404

    assert client.get(f"/c/{token}/history?before=abc").status_code == 400
    assert client.get(f"/c/{token}/history?before=0").status_code == 400

    page = load_history_page(db, certificate, None, limit=10)
    capped = client.get(f"/c/{token}/history?before={page.next_before}&limit=1000")
    assert capped.status_code == 200
    assert capped.text.count("<li") <= 50


def test_public_page_query_count_stable(app_client) -> None:
    client = app_client
    db = _db()
    hr, small = _issued(db)
    _add_transactions(db, small, hr, 20)
    _, huge = _issued(db, login="huge")
    _add_transactions(db, huge, hr, 500)

    def count_queries(token: str) -> int:
        assert database.engine is not None
        counter = {"n": 0}

        def before_cursor(*_args, **_kwargs) -> None:
            counter["n"] += 1

        event.listen(database.engine, "before_cursor_execute", before_cursor)
        try:
            response = client.get(f"/c/{token}")
            assert response.status_code == 200
        finally:
            event.remove(database.engine, "before_cursor_execute", before_cursor)
        return counter["n"]

    small_n = count_queries(small.token)
    huge_n = count_queries(huge.token)
    assert small_n == huge_n

    page = load_history_page(db, huge, None, limit=10)
    endpoint_n = {"n": 0}

    def before_cursor_endpoint(*_args, **_kwargs) -> None:
        endpoint_n["n"] += 1

    assert database.engine is not None
    event.listen(database.engine, "before_cursor_execute", before_cursor_endpoint)
    try:
        response = client.get(
            f"/c/{huge.token}/history?before={page.next_before}&limit=10"
        )
        assert response.status_code == 200
    finally:
        event.remove(database.engine, "before_cursor_execute", before_cursor_endpoint)
    assert endpoint_n["n"] <= 4


def test_undo_hidden_on_public_card_even_for_staff(app_client) -> None:
    """На ссылке /c/... отмены нет — даже если в сессии HR."""
    client = app_client
    db = _db()
    hr, certificate = _issued(db)
    debit = Transaction(
        certificate_id=certificate.id,
        actor_id=hr.id,
        tx_type=TxType.debit,
        amount=500,
        balance_after=0,
        comment="recent",
        created_at=datetime.now(timezone.utc),
    )
    db.add(debit)
    _add_transactions(db, certificate, hr, 12)
    db.commit()
    db.refresh(debit)

    page = load_history_page(db, certificate, None, limit=10)
    public = client.get(f"/c/{certificate.token}/history?before={page.next_before}")
    assert public.status_code == 200
    assert "Отменить" not in public.text
    assert "csrf_token" not in public.text

    settings = get_settings()
    _login(client, settings.hr_login, settings.hr_password)
    staff = client.get(f"/c/{certificate.token}/history?before={page.next_before}")
    assert staff.status_code == 200
    assert "Отменить" not in staff.text
    page_html = client.get(f"/c/{certificate.token}")
    assert page_html.status_code == 200
    assert "Отменить" not in page_html.text


def test_history_comment_escaped(app_client) -> None:
    client = app_client
    db = _db()
    hr, certificate = _issued(db)
    db.add(
        Transaction(
            certificate_id=certificate.id,
            actor_id=hr.id,
            tx_type=TxType.credit,
            amount=1,
            balance_after=1,
            comment="<script>alert(1)</script>",
        )
    )
    _add_transactions(db, certificate, hr, 11)
    db.commit()
    page = load_history_page(db, certificate, None, limit=10)
    response = client.get(f"/c/{certificate.token}/history?before={page.next_before}")
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_no_history_more_when_short(app_client) -> None:
    client = app_client
    db = _db()
    hr, certificate = _issued(db)
    _replace_transactions(db, certificate, hr, 5)
    response = client.get(f"/c/{certificate.token}")
    assert response.status_code == 200
    assert "history-more" not in response.text
    assert response.text.count("<li") <= 10


def test_html_size_and_queries_benchmark(app_client) -> None:
    """Замер до/после: страница /c/{token} при 500 операциях остаётся лёгкой."""
    client = app_client
    db = _db()
    hr, certificate = _issued(db)
    _add_transactions(db, certificate, hr, 500)
    card = load_card(db, certificate, "http://test", None)
    assert len(card.history) == HISTORY_PAGE_SIZE
    assert card.history_has_more is True

    assert database.engine is not None
    counter = {"n": 0}

    def before_cursor(*_args, **_kwargs) -> None:
        counter["n"] += 1

    event.listen(database.engine, "before_cursor_execute", before_cursor)
    try:
        response = client.get(f"/c/{certificate.token}")
    finally:
        event.remove(database.engine, "before_cursor_execute", before_cursor)
    assert response.status_code == 200
    assert "history-more" in response.text
    assert response.text.count('class="history-title"') == HISTORY_PAGE_SIZE
    # После пагинации HTML не тащит все 500 строк.
    assert len(response.content) < 200_000
    assert counter["n"] < 40
    print(
        f"history-paging /c/{{token}} 500 txs: html={len(response.content)}B queries={counter['n']}"
    )


def _db():
    assert database.SessionLocal is not None
    return database.SessionLocal()


def _issued(db, login: str = "holder") -> tuple[User, Certificate]:
    settings = get_settings()
    hr = db.scalar(select(User).where(User.login == settings.hr_login))
    if hr is None:
        hr = db.scalar(select(User).where(User.login == "hr"))
    if hr is None:
        hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Иван Петров",
            login=login,
            password="password1",
            nominal=1_000_000,
            balance=None,
            note="",
            expires_on=None,
        ),
        hr,
        sync_external=False,
    )
    return hr, issued.certificate


def _person(db, login: str, role: Role) -> User:
    from bonus.security import hash_password

    user = User(
        full_name=login,
        login=login,
        password_hash=hash_password("password1"),
        role=role,
        restaurant="",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _draft(nominal: int) -> IssueDraft:
    return IssueDraft(
        full_name="Иван Петров",
        login="",
        password="",
        nominal=nominal,
        balance=None,
        note="",
        expires_on=None,
    )


def _replace_transactions(
    db,
    certificate: Certificate,
    actor: User,
    count: int,
    *,
    prefix: str = "op",
) -> None:
    db.execute(delete(Transaction).where(Transaction.certificate_id == certificate.id))
    db.commit()
    _add_transactions(db, certificate, actor, count, prefix=prefix)


def _add_transactions(
    db,
    certificate: Certificate,
    actor: User,
    count: int,
    *,
    prefix: str = "op",
) -> None:
    for index in range(count):
        db.add(
            Transaction(
                certificate_id=certificate.id,
                actor_id=actor.id,
                tx_type=TxType.credit if index % 2 == 0 else TxType.burn,
                amount=100 + index,
                balance_after=1000,
                comment=f"{prefix}-{index}",
            )
        )
    db.commit()


def _login(client: TestClient, login: str, password: str) -> None:
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    response = client.post(
        "/login",
        data={
            "login": login,
            "password": password,
            "csrf_token": match.group(1),
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code in {303, 302}

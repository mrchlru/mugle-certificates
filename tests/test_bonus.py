"""Проверки выпуска, списания и разбора таблицы."""

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.catalog import (
    create_segment,
    ensure_default_segments,
    list_segments,
    save_template,
    update_segment,
    update_template,
)
from bonus.config import get_settings
from bonus.importer import parse_table
from bonus.models import AccrualPeriod, Base, Role, User
from bonus.security import hash_password
from bonus.services import (
    BonusError,
    IssueDraft,
    credit_certificate,
    debit_certificate,
    find_certificate,
    issue_certificate,
    set_blocked,
    set_certificate_expiry,
    undo_debit,
)
from bonus.values import format_date, parse_money


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def test_parse_money_reads_restaurant_amounts() -> None:
    assert parse_money("1 500") == 150_000
    assert parse_money("1500,50") == 150_050
    assert parse_money("10.000") == 1_000_000
    assert parse_money(10000) == 1_000_000
    assert parse_money("", blank_as_none=True) is None


def test_debit_stops_at_zero() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager = _person(db, "manager", Role.manager)
    issued = issue_certificate(db, _draft(1_000_000), hr)
    debit_certificate(db, issued.certificate, manager, 400_000, "обед")
    assert issued.certificate.balance == 600_000
    with pytest.raises(BonusError):
        debit_certificate(db, issued.certificate, manager, 700_000, "ещё")
    assert issued.certificate.balance == 600_000


def test_segment_edit_and_accrual_period() -> None:
    db = _db()
    ensure_default_segments(db)
    lunch = next(item for item in list_segments(db) if item.name == "Ланч")
    assert lunch.accrual_period == AccrualPeriod.daily
    assert lunch.requires_expiry is False
    barter = next(item for item in list_segments(db) if item.name.startswith("Бартер"))
    assert barter.requires_expiry is True
    birthday = next(item for item in list_segments(db) if item.name == "День рождения")
    assert birthday.requires_expiry is True
    segment = create_segment(db, "Временный", AccrualPeriod.none, requires_expiry=True)
    updated = update_segment(
        db,
        segment,
        name="Премия",
        accrual_period=AccrualPeriod.monthly,
        requires_expiry=False,
    )
    assert updated.name == "Премия"
    assert updated.accrual_period == AccrualPeriod.monthly
    assert updated.requires_expiry is False


def test_issue_with_segment_color_and_template() -> None:
    db = _db()
    ensure_default_segments(db)
    hr = _person(db, "hr", Role.hr)
    segment = create_segment(db, "Тестовый бартер", AccrualPeriod.none)
    template = save_template(
        db,
        name="Баннер",
        filename="banner.png",
        payload=_tiny_png(),
        content_type="image/png",
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Роман Мазов",
            login="mazov",
            password="",
            nominal=500_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
            template_id=template.id,
            color="#FF5A00",
        ),
        hr,
    )
    assert issued.certificate.color == "#FF5A00"
    assert issued.certificate.segment_id == segment.id
    assert issued.certificate.template_id == template.id
    assert template.image_path.endswith(".webp")
    stored = get_settings().data_path() / "uploads" / Path(template.image_path).name
    assert stored.is_file()
    assert stored.read_bytes()[:4] == b"RIFF"


def test_media_url_uses_s3_public_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "covers")
    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", "https://cdn.example/covers")
    monkeypatch.setenv("S3_PREFIX", "templates")
    from bonus.config import get_settings
    from bonus.storage import public_media_url

    get_settings.cache_clear()
    try:
        assert public_media_url("templates/a.webp") == "https://cdn.example/covers/templates/a.webp"
    finally:
        get_settings.cache_clear()


def test_certificate_expiry_can_be_set() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(db, _draft(500_000), hr)
    set_certificate_expiry(db, issued.certificate, date(2026, 12, 31))
    assert issued.certificate.expires_on == date(2026, 12, 31)
    assert format_date(issued.certificate.expires_on) == "31.12.2026"


def test_template_replace_deletes_old_file() -> None:
    db = _db()
    first = save_template(
        db,
        name="Старый",
        filename="old.png",
        payload=_tiny_png(),
        content_type="image/png",
    )
    old_key = first.image_path
    old_path = get_settings().data_path() / "uploads" / Path(old_key)
    assert old_path.is_file()
    updated = update_template(
        db,
        first,
        name="Новый",
        filename="new.png",
        payload=_tiny_png(),
        content_type="image/png",
    )
    assert updated.name == "Новый"
    assert updated.image_path != old_key
    assert not old_path.is_file()
    assert (get_settings().data_path() / "uploads" / Path(updated.image_path)).is_file()


def test_issue_without_login_stores_internal_fields() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Анна Козлова",
            login="",
            password="",
            nominal=100_000,
            balance=None,
            note="",
            expires_on=None,
            department="Кухня",
            job_title="Повар",
            phone="+7 900 111-22-33",
        ),
        hr,
    )
    assert issued.user.login != ""
    assert issued.user.department == "Кухня"
    assert issued.user.job_title == "Повар"
    assert issued.user.phone == "+7 900 111-22-33"


def test_credit_adds_bonuses_to_balance() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager = _person(db, "manager", Role.manager)
    issued = issue_certificate(db, _draft(1_000_000), hr)
    debit_certificate(db, issued.certificate, manager, 400_000, "обед")
    credit_certificate(db, issued.certificate, hr, 250_000, "премия")
    assert issued.certificate.balance == 850_000
    assert issued.certificate.nominal == 1_250_000


def test_blocked_and_expired_cannot_be_spent() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager = _person(db, "manager", Role.manager)
    issued = issue_certificate(db, _draft(500_000), hr)
    set_blocked(db, issued.certificate, hr, True)
    with pytest.raises(BonusError):
        debit_certificate(db, issued.certificate, manager, 100, "нет")
    set_blocked(db, issued.certificate, hr, False)
    issued.certificate.expires_on = date(2020, 1, 1)
    db.commit()
    with pytest.raises(BonusError):
        debit_certificate(db, issued.certificate, manager, 100, "нет")


def test_manager_undo_expires_and_hr_can_still_undo() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager = _person(db, "manager", Role.manager)
    issued = issue_certificate(db, _draft(500_000), hr)
    tx = debit_certificate(db, issued.certificate, manager, 100_000, "стол")
    tx.created_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    db.commit()
    with pytest.raises(BonusError):
        undo_debit(db, tx.id, manager, "поздно")
    undo_debit(db, tx.id, hr, "ошибка")
    assert issued.certificate.balance == 500_000
    with pytest.raises(BonusError):
        undo_debit(db, tx.id, hr, "ещё раз")


def test_import_keeps_spent_balance_and_skips_bad_row() -> None:
    payload = (
        "ФИО;Логин;Пароль;Номинал;Остаток\n"
        "Иванов Иван;ivanov;;10000;7500\n"
        "Битая строка;;;не-число;\n"
    ).encode()
    rows = parse_table("people.csv", payload)
    assert rows[0].draft is not None
    assert rows[0].draft.nominal == 1_000_000
    assert rows[0].draft.balance == 750_000
    assert rows[1].draft is None
    db = _db()
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(db, rows[0].draft, hr)
    assert issued.certificate.balance == 750_000
    assert issued.certificate.nominal == 1_000_000
    found = find_certificate(db, f"https://bonus.example/c/{issued.certificate.token}")
    assert found is not None
    assert found.id == issued.certificate.id
    by_short = find_certificate(db, issued.certificate.short_code)
    assert by_short is not None


def test_login_card_and_debit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bonus.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SEED_DEMO", "true")
    from bonus.config import get_settings

    get_settings.cache_clear()
    import bonus.main as main

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        hr = _login(client, "hr", "mugle-hr")
        issued = client.get("/hr/c/1")
        link = re.search(r'class="share">([^<]+)', issued.text)
        assert link is not None
        card = client.get(link.group(1).strip())
        assert card.status_code == 200
        assert "Мария Соколова" in card.text
        assert "<svg" in card.text
        short = re.search(r'class="wallet-code-number">([^<]+)', card.text)
        assert short is not None
        assert "wallet-card" in card.text
        _logout(client, hr.text)
        _login(client, "manager", "mugle-desk")
        opened = client.get("/desk", params={"code": short.group(1)}, follow_redirects=True)
        assert "Списать" in opened.text
        token = _csrf(opened.text)
        spent = client.post(
            opened.url.path,
            data={"csrf_token": token, "amount": "500", "comment": "ужин"},
            follow_redirects=True,
        )
        assert "Списано" in spent.text
        assert "16 000" in spent.text or "16000" in spent.text


def _login(client, login: str, password: str):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"login": login, "password": password, "csrf_token": _csrf(page.text), "next": "/"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    return response


def _logout(client, html: str) -> None:
    client.post("/logout", data={"csrf_token": _csrf(html)})


def _csrf(html: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert found is not None
    return found.group(1)


def _db() -> Session:
    assert database.SessionLocal is not None
    return database.SessionLocal()


def _person(db: Session, login: str, role: Role) -> User:
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


def _tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )

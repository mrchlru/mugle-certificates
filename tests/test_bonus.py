"""Проверки выпуска, списания и разбора таблицы."""

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.catalog import (
    create_segment,
    delete_segment,
    delete_template,
    ensure_default_segments,
    list_segments,
    list_templates,
    resolve_segment,
    resolve_template,
    save_template,
    update_segment,
    update_template,
)
from bonus.config import get_settings
from bonus.importer import parse_table
from bonus.models import AccrualPeriod, Base, Certificate, Role, Transaction, TxType, User
from bonus.security import hash_password
from bonus.accrual import apply_monthly_accruals
from bonus.services import (
    BonusError,
    IssueDraft,
    apply_batch_hr_adjustments,
    apply_expiry_writeoff,
    create_staff,
    credit_certificate,
    debit_certificate,
    delete_certificate,
    find_certificate,
    issue_certificate,
    reset_monthly_balance,
    set_blocked,
    set_certificate_expiry,
    set_certificate_single_use,
    undo_credit,
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
    one_shot = next(item for item in list_segments(db) if item.name == "Разовый")
    assert one_shot.single_use is True
    assert one_shot.requires_expiry is False
    segment = create_segment(
        db,
        "Временный",
        AccrualPeriod.none,
        requires_expiry=True,
        discount_percent=30,
    )
    assert segment.discount_percent == 30
    updated = update_segment(
        db,
        segment,
        name="Премия",
        accrual_period=AccrualPeriod.monthly,
        requires_expiry=False,
        discount_percent=15,
    )
    assert updated.name == "Премия"
    assert updated.accrual_period == AccrualPeriod.monthly
    assert updated.requires_expiry is False
    assert updated.discount_percent == 15


def test_monthly_reset_burns_leftover_and_restores_nominal() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager = _person(db, "manager", Role.manager)
    issued = issue_certificate(db, _draft(1_000_000), hr)
    debit_certificate(db, issued.certificate, manager, 400_000, "обед")
    assert issued.certificate.balance == 600_000
    reset_monthly_balance(db, issued.certificate, "Автоначисление: ежемесячно 2026-11")
    assert issued.certificate.balance == 1_000_000
    assert issued.certificate.nominal == 1_000_000
    burns = db.scalars(
        select(Transaction).where(
            Transaction.certificate_id == issued.certificate.id,
            Transaction.tx_type == TxType.burn,
        )
    ).all()
    assert any(row.amount == 600_000 for row in burns)
    markers = db.scalars(
        select(Transaction).where(
            Transaction.certificate_id == issued.certificate.id,
            Transaction.tx_type == TxType.credit,
            Transaction.comment == "Автоначисление: ежемесячно 2026-11",
        )
    ).all()
    assert len(markers) == 1


def test_monthly_accrual_skips_issue_month() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    ensure_default_segments(db)
    segment = next(
        item for item in list_segments(db) if item.accrual_period == AccrualPeriod.monthly
    )
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Сотрудник",
            login="",
            password="",
            nominal=500_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
        ),
        hr,
    )
    now = datetime.now().astimezone()
    assert apply_monthly_accruals(db, now) == 0
    issued.certificate.created_at = datetime(
        now.year, now.month, 1, tzinfo=timezone.utc
    ) - timedelta(days=32)
    db.commit()
    db.refresh(issued.certificate)
    done = apply_monthly_accruals(db, now)
    assert done == 1
    assert issued.certificate.balance == 500_000
    assert apply_monthly_accruals(db, now) == 0


def test_admin_can_soft_delete_certificate() -> None:
    db = _db()
    admin = _person(db, "admin", Role.admin)
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(db, _draft(200_000), hr)
    cert_id = issued.certificate.id
    public_number = delete_certificate(db, issued.certificate, admin)
    assert public_number
    cert = db.get(Certificate, cert_id)
    assert cert is not None
    assert cert.is_deleted is True
    assert cert.status.value == "blocked"
    with pytest.raises(BonusError):
        delete_certificate(db, cert, admin)
    with pytest.raises(BonusError):
        delete_certificate(db, issue_certificate(db, _draft(100_000), hr).certificate, hr)


def test_single_use_burns_remainder_after_first_debit() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager = _person(db, "manager", Role.manager)
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Гость Разовый",
            login="",
            password="",
            nominal=300_000,
            balance=None,
            note="",
            expires_on=None,
            single_use=True,
        ),
        hr,
    )
    assert issued.certificate.single_use is True
    assert issued.certificate.balance == 300_000
    debit_certificate(db, issued.certificate, manager, 100_000, "обед")
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 0
    burns = db.scalars(
        select(Transaction).where(
            Transaction.certificate_id == issued.certificate.id,
            Transaction.tx_type == TxType.burn,
        )
    ).all()
    assert len(burns) == 1
    assert burns[0].amount == 200_000
    with pytest.raises(BonusError):
        debit_certificate(db, issued.certificate, manager, 100, "ещё")


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
        logo_filename="logo.png",
        logo_payload=_tiny_png(),
        logo_content_type="image/png",
        color="#FF5A00",
        segment_id=segment.id,
    )
    assert template.color == "#FF5A00"
    assert template.segment_id == segment.id
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
            color=template.color,
        ),
        hr,
    )
    assert issued.certificate.color == "#FF5A00"
    assert issued.certificate.segment_id == segment.id
    assert issued.certificate.template_id == template.id
    assert template.image_path.endswith(".webp")
    assert template.logo_path.endswith(".webp")
    assert template.logo_path.startswith("logo-") or "logo-" in Path(template.logo_path).name


def test_soft_delete_keeps_issued_card_links() -> None:
    """Удаление из каталога не ломает выпущенные карты: настройки и визуал по FK."""
    db = _db()
    ensure_default_segments(db)
    hr = _person(db, "hr", Role.hr)
    admin = _person(db, "admin", Role.admin)
    segment = create_segment(db, "Временный сегмент", AccrualPeriod.none)
    template = save_template(
        db,
        name="Временный баннер",
        filename="banner.png",
        payload=_tiny_png(),
        content_type="image/png",
        color="#112233",
        segment_id=segment.id,
    )
    banner_path = template.image_path
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Старая Карта",
            login="",
            password="",
            nominal=100_00,
            balance=None,
            note="",
            expires_on=None,
            segment_id=segment.id,
            template_id=template.id,
            color=template.color,
        ),
        hr,
        sync_external=False,
    )
    cert_id = issued.certificate.id
    delete_template(db, template, admin)
    db.refresh(template)
    assert template.is_deleted is True
    assert template.is_active is False
    assert template.image_path == banner_path
    cert = db.get(Certificate, cert_id)
    assert cert is not None
    assert cert.template_id == template.id
    assert cert.segment_id == segment.id
    assert list_templates(db) == []
    with pytest.raises(BonusError):
        resolve_template(db, template.id)

    delete_segment(db, segment, admin)
    db.refresh(segment)
    assert segment.is_deleted is True
    assert "удалён" in segment.name
    cert = db.get(Certificate, cert_id)
    assert cert is not None
    assert cert.segment_id == segment.id
    assert segment.id not in {item.id for item in list_segments(db, active_only=False)}
    with pytest.raises(BonusError):
        resolve_segment(db, segment.id)
    create_segment(db, "Временный сегмент", AccrualPeriod.none)


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


def test_expired_certificate_writes_off_balance() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(db, _draft(500_000), hr)
    set_certificate_expiry(db, issued.certificate, date(2020, 1, 1))
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 0
    assert apply_expiry_writeoff(db, issued.certificate) is False
    tx = db.scalar(
        select(Transaction).where(
            Transaction.certificate_id == issued.certificate.id,
            Transaction.tx_type == TxType.expire,
        )
    )
    assert tx is not None
    assert tx.amount == 500_000
    assert "годен до" in tx.comment


def test_renew_expired_card_restores_last_balance() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager = _person(db, "mgr", Role.manager)
    issued = issue_certificate(db, _draft(500_000), hr, sync_external=False)
    debit_certificate(
        db, issued.certificate, manager, 150_000, "трата", enforce_weekdays=False
    )
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 350_000
    set_certificate_expiry(db, issued.certificate, date(2020, 1, 1), actor=hr)
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 0

    restored = set_certificate_expiry(db, issued.certificate, date(2030, 12, 31), actor=hr)
    db.refresh(issued.certificate)
    assert restored == 350_000
    assert issued.certificate.balance == 350_000
    # Повторное сохранение того же срока не дублирует начисление.
    assert set_certificate_expiry(db, issued.certificate, date(2031, 1, 1), actor=hr) == 0
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 350_000


def test_template_replace_deletes_old_file() -> None:
    db = _db()
    first = save_template(
        db,
        name="Старый",
        filename="old.png",
        payload=_tiny_png(),
        content_type="image/png",
        logo_filename="old-logo.png",
        logo_payload=_tiny_png(),
        logo_content_type="image/png",
    )
    old_key = first.image_path
    old_logo = first.logo_path
    old_path = get_settings().data_path() / "uploads" / Path(old_key)
    old_logo_path = get_settings().data_path() / "uploads" / Path(old_logo)
    assert old_path.is_file()
    assert old_logo_path.is_file()
    updated = update_template(
        db,
        first,
        name="Новый",
        filename="new.png",
        payload=_tiny_png(),
        content_type="image/png",
        logo_filename="new-logo.png",
        logo_payload=_tiny_png(),
        logo_content_type="image/png",
    )
    assert updated.name == "Новый"
    assert updated.image_path != old_key
    assert updated.logo_path != old_logo
    assert not old_path.is_file()
    assert not old_logo_path.is_file()
    assert (get_settings().data_path() / "uploads" / Path(updated.image_path)).is_file()
    assert (get_settings().data_path() / "uploads" / Path(updated.logo_path)).is_file()


def test_staff_and_debit_keeps_restaurant() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    manager, _plain = create_staff(
        db,
        first_name="Игорь",
        last_name="Смирнов",
        phone="+79001234567",
        password="password1",
        role=Role.manager,
        restaurant="Си мама",
    )
    assert manager.restaurant == "Си мама"
    assert manager.phone == "+79001234567"
    issued = issue_certificate(db, _draft(500_000), hr)
    tx = debit_certificate(db, issued.certificate, manager, 100_000, "обед")
    assert tx.restaurant == "Си мама"


def test_issue_with_discount_percent() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name="Гость",
            login="",
            password="",
            nominal=200_000,
            balance=None,
            note="",
            expires_on=None,
            discount_percent=30,
        ),
        hr,
    )
    assert issued.certificate.discount_percent == 30


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


def test_credit_with_grow_nominal_false_keeps_accrual_cap() -> None:
    """Ручная корректировка баланса (как из HR) не должна трогать лимит автоначисления."""
    db = _db()
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(db, _draft(1_000_000), hr)
    credit_certificate(
        db, issued.certificate, hr, 250_000, "долили без смены лимита", grow_nominal=False
    )
    assert issued.certificate.balance == 1_250_000
    assert issued.certificate.nominal == 1_000_000


def test_batch_hr_adjustments_sets_limit_and_credits_without_growing_cap() -> None:
    """Пакетная операция: новый ежемесячный лимит + начисление на ту же часть."""
    db = _db()
    hr = _person(db, "hr-batch", Role.hr)
    segment = create_segment(db, "Месячный пакет (тест)", AccrualPeriod.monthly)
    draft = IssueDraft(
        full_name="Иван Петров",
        login="",
        password="",
        nominal=4_000_00,
        balance=None,
        note="",
        expires_on=None,
        segment_id=segment.id,
    )
    first = issue_certificate(db, draft, hr, sync_external=False).certificate
    second = issue_certificate(db, draft, hr, sync_external=False).certificate

    result = apply_batch_hr_adjustments(
        db,
        hr,
        [first.id, second.id],
        limit_monthly=6_000_00,
        credit_amount=2_000_00,
        credit_period=AccrualPeriod.monthly,
        comment="пакетный тест",
    )
    assert len(result.items) == 2
    assert all(item.ok for item in result.items)
    assert result.limits_applied == 2
    assert result.credits_applied == 2

    db.refresh(first)
    db.refresh(second)
    assert first.balance == 6_000_00
    assert second.balance == 6_000_00
    assert first.nominal == 6_000_00
    assert second.nominal == 6_000_00


def test_batch_hr_adjustments_sets_single_use_flag() -> None:
    db = _db()
    hr = _person(db, "hr-su", Role.hr)
    first = issue_certificate(db, _draft(1_000_00), hr, sync_external=False).certificate
    second = issue_certificate(db, _draft(1_000_00), hr, sync_external=False).certificate
    assert first.single_use is False

    on = apply_batch_hr_adjustments(db, hr, [first.id, second.id], single_use=True)
    assert on.single_use_applied == 2
    assert all(item.ok for item in on.items)
    db.refresh(first)
    db.refresh(second)
    assert first.single_use is True
    assert second.single_use is True

    off = apply_batch_hr_adjustments(db, hr, [first.id], single_use=False)
    assert off.single_use_applied == 1
    db.refresh(first)
    assert first.single_use is False


def test_set_certificate_single_use_on_card() -> None:
    db = _db()
    hr = _person(db, "hr-su2", Role.hr)
    issued = issue_certificate(db, _draft(1_000_00), hr, sync_external=False).certificate
    assert issued.single_use is False
    set_certificate_single_use(db, issued, single_use=True)
    db.refresh(issued)
    assert issued.single_use is True
    set_certificate_single_use(db, issued, single_use=False)
    db.refresh(issued)
    assert issued.single_use is False


def test_unavailable_reason_covers_deleted_blocked_expired() -> None:
    from bonus.routes_card import _unavailable_reason

    db = _db()
    hr = _person(db, "hr", Role.hr)
    admin = _person(db, "admin", Role.admin)
    active = issue_certificate(db, _draft(100_00), hr, sync_external=False).certificate
    assert _unavailable_reason(active) is None

    blocked = issue_certificate(db, _draft(100_00), hr, sync_external=False).certificate
    set_blocked(db, blocked, hr, True)
    assert _unavailable_reason(blocked) == "blocked"

    expired = issue_certificate(db, _draft(100_00), hr, sync_external=False).certificate
    set_certificate_expiry(db, expired, date(2020, 1, 1))
    assert _unavailable_reason(expired) == "expired"

    deleted = issue_certificate(db, _draft(100_00), hr, sync_external=False).certificate
    delete_certificate(db, deleted, admin)
    db.refresh(deleted)
    assert _unavailable_reason(deleted) == "deleted"


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


def test_undo_duplicate_auto_credit_keeps_balance() -> None:
    """Отмена одного из двух credit с одним маркером не обнуляет часть."""
    db = _db()
    admin = _person(db, "admin", Role.admin)
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(db, _draft(500_000), hr)
    marker = "Автоначисление: еженедельно 2026-W46"
    credit_certificate(
        db,
        issued.certificate,
        None,
        130_000,
        marker,
        grow_nominal=False,
    )
    credit_certificate(
        db,
        issued.certificate,
        None,
        130_000,
        marker,
        grow_nominal=False,
    )
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 760_000
    dup = db.scalars(
        select(Transaction)
        .where(
            Transaction.certificate_id == issued.certificate.id,
            Transaction.comment == marker,
        )
        .order_by(Transaction.id.desc())
    ).first()
    assert dup is not None
    undo_credit(db, dup.id, admin, "дубль")
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 760_000


def test_admin_undo_credit_idempotent() -> None:
    db = _db()
    admin = _person(db, "admin", Role.admin)
    hr = _person(db, "hr", Role.hr)
    issued = issue_certificate(db, _draft(500_000), hr)
    credit_tx = credit_certificate(db, issued.certificate, hr, 100_000, "ошибочное")
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 600_000
    undo_credit(db, credit_tx.id, admin, "дубль")
    db.refresh(issued.certificate)
    assert issued.certificate.balance == 500_000
    reversal = db.scalar(
        select(Transaction).where(Transaction.reversal_of_id == credit_tx.id)
    )
    assert reversal is not None
    assert reversal.tx_type == TxType.debit
    with pytest.raises(BonusError):
        undo_credit(db, credit_tx.id, admin, "ещё раз")
    with pytest.raises(BonusError):
        undo_credit(db, credit_tx.id, hr, "hr не может")


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
    assert issued.certificate.short_code.isdigit()
    assert len(issued.certificate.short_code) == 8


def test_hr_issue_form_redirects_without_500(tmp_path, monkeypatch) -> None:
    """Регрессия: после выпуска аудит не падает на несуществующем nominal_value."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'issue.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-issue")
    monkeypatch.setenv("SEED_DEMO", "true")
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
    from bonus.config import get_settings

    get_settings.cache_clear()
    import bonus.main as main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        _login(client, "hr", "mugle-hr")
        form = client.get("/hr/new")
        assert form.status_code == 200
        match = re.search(r'name="segment_id"[^>]*>[\s\S]*?<option value="(\d+)"', form.text)
        assert match is not None
        response = client.post(
            "/hr/new",
            data={
                "csrf_token": _csrf(form.text),
                "full_name": "Петров Пётр",
                "company": "ООО Тест",
                "department": "Хачапури",
                "job_title": "Официант",
                "phone": "",
                "ledger_sheet": "employee",
                "segment_id": match.group(1),
                "template_id": "0",
                "nominal": "10000",
                "note": "",
                "expires_on": "",
                "discount_percent": "",
                "color": "#00A8A8",
                "use_days": ["1", "2", "3", "4", "5", "6", "7"],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/hr/c/")


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
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (0, 168, 168)).save(buffer, format="PNG")
    return buffer.getvalue()

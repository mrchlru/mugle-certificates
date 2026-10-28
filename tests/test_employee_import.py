"""Массовая загрузка карт сотрудников из Excel."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.catalog import create_segment, save_template
from bonus.config import get_settings
from bonus.employee_import import (
    KIND_COMBO_DAY,
    KIND_COMBO_WEEK,
    KIND_EMPLOYEE,
    commit_employee_import,
    detect_employee_file_kind,
    preview_employee_workbook,
)
from bonus.models import AccrualPeriod, Base, Role, User
from bonus.parts import list_parts
from bonus.security import hash_password
from bonus.services import BonusError
from bonus.weekdays import parse_weekday_labels


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    yield
    get_settings.cache_clear()


def test_detect_kinds_by_headers() -> None:
    assert (
        detect_employee_file_kind(["ФИО", "Номинал, ₽ в месяц", "ООО"])
        == KIND_EMPLOYEE
    )
    assert (
        detect_employee_file_kind(["ФИО", "Ланч, ₽ в день", "Карта сотрудника, ₽ в месяц"])
        == KIND_COMBO_DAY
    )
    assert (
        detect_employee_file_kind(
            ["ФИО", "Недельные бонусы, ₽ в неделю", "Карта сотрудника, ₽ в месяц"]
        )
        == KIND_COMBO_WEEK
    )
    with pytest.raises(BonusError):
        detect_employee_file_kind(["ФИО", "Номинал"])


def test_parse_weekday_labels() -> None:
    assert parse_weekday_labels("Пн,Вт,Ср,Чт,Пт") == "12345"
    assert parse_weekday_labels("") == "1234567"
    with pytest.raises(ValueError):
        parse_weekday_labels("Пн,Xxx")


def test_employee_month_import_creates_card() -> None:
    db = _db()
    hr = _seed_catalog(db)
    payload = _workbook(
        "Карты",
        [
            "ФИО",
            "ООО",
            "Ресторан / офис",
            "Должность",
            "Телефон",
            "Номинал, ₽ в месяц",
            "Дни использования",
            "Шаблон баннера",
            "Процент на карточке",
            "Цвет карточки",
            "Комментарий",
        ],
        [
            [
                "Иванов Иван",
                "МУГЛЕ",
                "ХВВ",
                "Официант",
                "+79990001122",
                10000,
                "Пн,Вт,Ср,Чт,Пт",
                "",
                "",
                "",
                "тест",
            ]
        ],
    )
    preview = preview_employee_workbook(db, filename="t.xlsx", payload=payload)
    assert preview.kind == KIND_EMPLOYEE
    assert preview.ok_rows == 1
    results, ids = commit_employee_import(
        db,
        batch_id=preview.batch_id,
        actor=hr,
        base_url="https://example.test",
    )
    assert len(ids) == 1
    assert results[0].status == "создана"
    parts = list_parts(db, ids[0])
    assert len(parts) == 1
    assert parts[0].nominal == 1_000_000
    assert parts[0].accrual_period == AccrualPeriod.monthly


def test_combo_day_and_week_import() -> None:
    db = _db()
    hr = _seed_catalog(db)
    day_payload = _workbook(
        "Карты",
        [
            "ФИО",
            "ООО",
            "Ресторан / офис",
            "Должность",
            "Телефон",
            "Карта сотрудника, ₽ в месяц",
            "Ланч, ₽ в день",
            "Дни использования",
            "Дни начисления · Ланч",
            "Шаблон баннера",
            "Процент на карточке",
            "Цвет карточки",
            "Комментарий",
        ],
        [["Петрова Анна", "МУГЛЕ", "Офис", "HR", "", 8000, 500, "", "Пн,Вт,Ср,Чт,Пт", "", "", "", ""]],
    )
    preview = preview_employee_workbook(db, filename="day.xlsx", payload=day_payload)
    assert preview.kind == KIND_COMBO_DAY
    results, ids = commit_employee_import(
        db, batch_id=preview.batch_id, actor=hr, base_url="https://example.test"
    )
    assert results[0].status == "создана"
    parts = {part.accrual_period: part for part in list_parts(db, ids[0])}
    assert parts[AccrualPeriod.monthly].nominal == 800_000
    assert parts[AccrualPeriod.daily].nominal == 50_000

    week_payload = _workbook(
        "Карты",
        [
            "ФИО",
            "ООО",
            "Ресторан / офис",
            "Должность",
            "Телефон",
            "Карта сотрудника, ₽ в месяц",
            "Недельные бонусы, ₽ в неделю",
            "Дни использования",
            "Шаблон баннера",
            "Процент на карточке",
            "Цвет карточки",
            "Комментарий",
        ],
        [["Сидоров Пётр", "МУГЛЕ", "Офис", "Менеджер", "", 9000, 2000, "", "", "", "", ""]],
    )
    preview2 = preview_employee_workbook(db, filename="week.xlsx", payload=week_payload)
    assert preview2.kind == KIND_COMBO_WEEK
    results2, ids2 = commit_employee_import(
        db, batch_id=preview2.batch_id, actor=hr, base_url="https://example.test"
    )
    assert results2[0].status == "создана"
    parts2 = {part.accrual_period: part for part in list_parts(db, ids2[0])}
    assert parts2[AccrualPeriod.monthly].nominal == 900_000
    assert parts2[AccrualPeriod.weekly].nominal == 200_000
    assert parts2[AccrualPeriod.weekly].accrual_days == "5"


def test_row_errors_and_missing_weekly_segment() -> None:
    db = _db()
    _seed_catalog(db, with_weekly=False)
    payload = _workbook(
        "Карты",
        [
            "ФИО",
            "ООО",
            "Ресторан / офис",
            "Должность",
            "Телефон",
            "Карта сотрудника, ₽ в месяц",
            "Недельные бонусы, ₽ в неделю",
            "Дни использования",
            "Шаблон баннера",
            "Процент на карточке",
            "Цвет карточки",
            "Комментарий",
        ],
        [["А", "Б", "В", "Г", "", 1, 1, "", "", "", "", ""]],
    )
    with pytest.raises(BonusError, match="Недельные бонусы"):
        preview_employee_workbook(db, filename="week.xlsx", payload=payload)

    bad = _workbook(
        "Карты",
        [
            "ФИО",
            "ООО",
            "Ресторан / офис",
            "Должность",
            "Телефон",
            "Номинал, ₽ в месяц",
            "Дни использования",
            "Шаблон баннера",
            "Процент на карточке",
            "Цвет карточки",
            "Комментарий",
        ],
        [
            ["Иванов Иван", "МУГЛЕ", "ХВВ", "Официант", "", "abc", "", "", "150", "красный", ""],
        ],
    )
    preview = preview_employee_workbook(db, filename="bad.xlsx", payload=bad)
    assert preview.error_rows == 1
    assert preview.ok_rows == 0
    assert "Номинал" in preview.rows[0].error
    assert "Процент" in preview.rows[0].error
    assert "Цвет" in preview.rows[0].error


def test_rejects_non_xlsx_and_wrong_sheet() -> None:
    db = _db()
    _seed_catalog(db)
    with pytest.raises(BonusError, match="xlsx"):
        preview_employee_workbook(db, filename="a.csv", payload=b"a,b\n")
    payload = _workbook("Пример", ["ФИО", "Номинал, ₽ в месяц"], [["Иван", 1]])
    with pytest.raises(BonusError, match="Карты"):
        preview_employee_workbook(db, filename="t.xlsx", payload=payload)
    empty = _workbook(
        "Карты",
        [
            "ФИО",
            "ООО",
            "Ресторан / офис",
            "Должность",
            "Номинал, ₽ в месяц",
        ],
        [],
    )
    with pytest.raises(BonusError, match="нет заполненных"):
        preview_employee_workbook(db, filename="empty.xlsx", payload=empty)


def test_duplicate_skipped_by_default() -> None:
    db = _db()
    hr = _seed_catalog(db)
    headers = [
        "ФИО",
        "ООО",
        "Ресторан / офис",
        "Должность",
        "Телефон",
        "Номинал, ₽ в месяц",
        "Дни использования",
        "Шаблон баннера",
        "Процент на карточке",
        "Цвет карточки",
        "Комментарий",
    ]
    row = ["Иванов Иван", "МУГЛЕ", "ХВВ", "Официант", "", 5000, "", "", "", "", ""]
    first = preview_employee_workbook(
        db, filename="a.xlsx", payload=_workbook("Карты", headers, [row])
    )
    commit_employee_import(
        db, batch_id=first.batch_id, actor=hr, base_url="https://example.test"
    )
    second = preview_employee_workbook(
        db, filename="b.xlsx", payload=_workbook("Карты", headers, [row])
    )
    assert second.duplicate_rows == 1
    assert second.ok_rows == 0
    results, ids = commit_employee_import(
        db, batch_id=second.batch_id, actor=hr, base_url="https://example.test"
    )
    assert ids == []
    assert results[0].status == "пропущена"



def _db() -> Session:
    assert database.SessionLocal is not None
    return database.SessionLocal()


def _person(db: Session, login: str, role: Role) -> User:
    user = User(
        full_name="HR Test",
        login=login,
        password_hash=hash_password("secret123"),
        role=role,
        restaurant="",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_catalog(db: Session, *, with_weekly: bool = True) -> User:
    hr = _person(db, "hr", Role.hr)
    employee = create_segment(db, "Карта сотрудника", AccrualPeriod.monthly)
    lunch = create_segment(db, "Ланч", AccrualPeriod.daily, accrual_days="12345")
    png = _tiny_png()
    save_template(
        db,
        name="Карта сотрудника",
        filename="e.png",
        payload=png,
        content_type="image/png",
        segment_id=employee.id,
    )
    save_template(
        db,
        name="Комбо карта",
        filename="c.png",
        payload=png,
        content_type="image/png",
        segment_id=employee.id,
        segment2_id=lunch.id,
    )
    if with_weekly:
        weekly = create_segment(
            db, "Недельные бонусы", AccrualPeriod.weekly, accrual_days="5"
        )
        save_template(
            db,
            name="Комбо карта: месяц + неделя",
            filename="w.png",
            payload=png,
            content_type="image/png",
            segment_id=employee.id,
            segment2_id=weekly.id,
        )
    return hr


def _workbook(sheet_name: str, headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = sheet_name
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _tiny_png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 168, 168)).save(buffer, format="PNG")
    return buffer.getvalue()


"""Коннект «Спасибо»: preview, commit, антидубли."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import bonus.db as database
from bonus.catalog import ensure_default_segments, list_segments
from bonus.models import Base
from bonus.services import IssueDraft, issue_certificate
from bonus.spasibo_connect import commit_spasibo_connect, preview_spasibo_workbook


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    database._ENGINE = engine
    database._SESSION_FACTORY = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    ensure_default_segments(session)
    yield session
    session.close()


def _workbook_bytes(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Коннект"
    for row in rows:
        sheet.append(row)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _issue_employee(db: Session, name: str):
    ensure_default_segments(db)
    employee = next(s for s in list_segments(db) if s.name == "Карта сотрудника")
    issued = issue_certificate(
        db,
        IssueDraft(
            full_name=name,
            login="",
            password="",
            nominal=10_000,
            balance=None,
            note="",
            expires_on=None,
            segment_id=employee.id,
            segment_name=employee.name,
            company="ООО Тест",
            department="Офис",
            ledger_sheet="employee",
        ),
        None,
        sync_external=False,
    )
    return issued.user, issued.certificate.public_number


def test_spasibo_connect_links_holder(db: Session) -> None:
    holder, public_number = _issue_employee(db, "Иван Тестов")
    payload = _workbook_bytes(
        [
            ["ID Спасибо", "Номер карты Teams", "Фамилия", "Имя"],
            [42, public_number, "Тестов", "Иван"],
        ]
    )
    preview = preview_spasibo_workbook(db, filename="c.xlsx", payload=payload)
    assert preview.ok_rows == 1
    results = commit_spasibo_connect(
        db,
        batch_id=preview.batch_id,
        base_url="https://bonus.test",
    )
    assert results[0].status == "привязано"
    db.refresh(holder)
    assert holder.spasibo_user_id == "42"
    assert holder.spasibo_certificate_id is not None


def test_spasibo_connect_rejects_duplicate_id_in_file(db: Session) -> None:
    _, pub = _issue_employee(db, "Сотрудник Альфа")
    _, pub2 = _issue_employee(db, "Сотрудник Бета")
    payload = _workbook_bytes(
        [
            ["ID Спасибо", "Номер карты Teams"],
            [7, pub],
            [7, pub2],
        ]
    )
    preview = preview_spasibo_workbook(db, filename="c.xlsx", payload=payload)
    assert preview.error_rows == 0
    assert preview.duplicate_rows >= 1


def test_spasibo_connect_idempotent(db: Session) -> None:
    holder, public_number = _issue_employee(db, "Повтор")
    payload = _workbook_bytes([["ID Спасибо", "Номер карты Teams"], [99, public_number]])
    preview = preview_spasibo_workbook(db, filename="c.xlsx", payload=payload)
    commit_spasibo_connect(db, batch_id=preview.batch_id, base_url="https://bonus.test")
    preview2 = preview_spasibo_workbook(db, filename="c.xlsx", payload=payload)
    assert preview2.duplicate_rows == 1
    results = commit_spasibo_connect(
        db,
        batch_id=preview2.batch_id,
        base_url="https://bonus.test",
        apply_duplicates=True,
    )
    assert results[0].status == "обновлено"
    db.refresh(holder)
    assert holder.spasibo_user_id == "99"


def test_spasibo_connect_accepts_first_sheet_with_headers(db: Session) -> None:
    holder, public_number = _issue_employee(db, "Лист Один")
    payload = _workbook_bytes(
        [
            ["ID Спасибо", "Номер карты Teams"],
            [11, public_number],
        ]
    )
    book = __import__("io").BytesIO(payload)
    from openpyxl import load_workbook

    wb = load_workbook(book)
    wb.active.title = "Sheet1"
    out = __import__("io").BytesIO()
    wb.save(out)
    preview = preview_spasibo_workbook(db, filename="c.xlsx", payload=out.getvalue())
    assert preview.ok_rows == 1


def test_spasibo_connect_conflict_two_holders(db: Session) -> None:
    _, pub1 = _issue_employee(db, "Первый")
    _, pub2 = _issue_employee(db, "Второй")
    payload1 = _workbook_bytes([["ID Спасибо", "Номер карты Teams"], [500, pub1]])
    p1 = preview_spasibo_workbook(db, filename="a.xlsx", payload=payload1)
    commit_spasibo_connect(db, batch_id=p1.batch_id, base_url="https://bonus.test")
    payload2 = _workbook_bytes([["ID Спасибо", "Номер карты Teams"], [500, pub2]])
    p2 = preview_spasibo_workbook(db, filename="b.xlsx", payload=payload2)
    assert p2.error_rows == 1
    assert "уже у другого держателя" in p2.rows[0].error

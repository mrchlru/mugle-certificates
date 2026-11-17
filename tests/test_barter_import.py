"""Импорт бартерных сертификатов."""

from datetime import date
from io import BytesIO
from pathlib import Path

import bonus.db as database
import pytest
from openpyxl import Workbook
from sqlalchemy.orm import Session

from bonus.barter_import import (
    build_barter_import_report,
    build_sample_workbook,
    commit_barter_import,
    preview_barter_workbook,
)
from bonus.catalog import create_segment, save_template
from bonus.config import get_settings
from bonus.models import AccrualPeriod, Base, Certificate, Role, User
from bonus.security import hash_password


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


def _workbook_bytes(rows: list[tuple[object, ...]]) -> bytes:
    wb = Workbook()
    sh = wb.active
    assert sh is not None
    sh.title = "Карты"
    for row in rows:
        sh.append(list(row))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_barter_preview_and_commit() -> None:
    db = _db()
    admin = User(login="a", password_hash=hash_password("x"), full_name="Admin", role=Role.admin)
    db.add(admin)
    db.commit()
    segment = create_segment(db, "Бартер на все рестораны", AccrualPeriod.yearly)
    png = _tiny_png()
    template = save_template(
        db,
        name="Бартер на все рестораны",
        filename="barter.png",
        payload=png,
        content_type="image/png",
        segment_id=segment.id,
    )
    payload = _workbook_bytes(
        [
            (
                "ФИО",
                "ООО",
                "Ресторан / офис",
                "Должность",
                "Сегмент",
                "Срок действия",
                "Номинал",
                "Комментарий",
                "Телефон",
            ),
            (
                "Иванов Иван Иванович",
                "МУГЛЕ",
                "ХВВ",
                "Официант",
                "Бартер на все рестораны",
                date(2026, 12, 31),
                5000,
                "Бартер",
                "",
            ),
        ]
    )
    preview = preview_barter_workbook(db, filename="t.xlsx", payload=payload)
    assert preview.ok_rows == 1
    results, ids = commit_barter_import(
        db,
        batch_id=preview.batch_id,
        actor=admin,
        base_url="https://example.test",
    )
    assert len(ids) == 1
    assert results[0].status == "создана"
    assert results[0].link.startswith("https://example.test/c/")
    cert = db.get(Certificate, ids[0])
    assert cert is not None
    assert cert.ledger_sheet == "barter"
    assert cert.template_id == template.id
    report = build_barter_import_report(results)
    assert len(report) > 1000


def test_barter_unknown_segment() -> None:
    db = _db()
    payload = _workbook_bytes(
        [
            (
                "ФИО",
                "ООО",
                "Ресторан / офис",
                "Должность",
                "Сегмент",
                "Срок действия",
                "Номинал",
            ),
            (
                "Сидоров Пётр",
                "МУГЛЕ",
                "ХВВ",
                "Повар",
                "Несуществующий сегмент",
                date(2026, 6, 1),
                1000,
            ),
        ]
    )
    preview = preview_barter_workbook(db, filename="bad.xlsx", payload=payload)
    assert preview.ok_rows == 0
    assert preview.error_rows == 1
    assert "не найден" in preview.rows[0].error.casefold()


def test_barter_sample_builds() -> None:
    data = build_sample_workbook()
    assert len(data) > 1000


def _db() -> Session:
    assert database.SessionLocal is not None
    return database.SessionLocal()


def _tiny_png() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (40, 120, 80)).save(buffer, format="PNG")
    return buffer.getvalue()

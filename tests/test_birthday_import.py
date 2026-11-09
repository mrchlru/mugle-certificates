"""Импорт сертификатов на день рождения."""

from datetime import date
from io import BytesIO
from pathlib import Path

import bonus.db as database
import pytest
from openpyxl import Workbook
from sqlalchemy.orm import Session

from bonus.birthday_import import (
    build_sample_workbook,
    commit_birthday_import,
    preview_birthday_workbook,
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


def test_birthday_preview_and_commit() -> None:
    db = _db()
    admin = User(login="a", password_hash=hash_password("x"), full_name="Admin", role=Role.admin)
    db.add(admin)
    db.commit()
    birthday = create_segment(db, "День рождения", AccrualPeriod.yearly)
    png = _tiny_png()
    template = save_template(
        db,
        name="День рождения",
        filename="bd.png",
        payload=png,
        content_type="image/png",
        segment_id=birthday.id,
    )
    payload = _workbook_bytes(
        [
            (
                "ФИО",
                "ООО",
                "Ресторан / офис",
                "Должность",
                "Дата рождения",
                "Срок действия",
                "Номинал",
                "Комментарий",
                "Телефон",
            ),
            (
                "Петрова Анна Сергеевна",
                "ТИРАМИСУ",
                "ХВВ",
                "Официант",
                date(1995, 5, 15),
                date(2026, 12, 31),
                3000,
                "ДР",
                "",
            ),
        ]
    )
    preview = preview_birthday_workbook(db, filename="t.xlsx", payload=payload)
    assert preview.ok_rows == 1
    results, ids = commit_birthday_import(
        db,
        batch_id=preview.batch_id,
        actor=admin,
        base_url="https://example.test",
    )
    assert len(ids) == 1
    assert results[0].status == "создана"
    cert = db.get(Certificate, ids[0])
    assert cert is not None
    assert cert.ledger_sheet == "birthday"
    assert cert.template_id == template.id


def test_birthday_sample_builds() -> None:
    data = build_sample_workbook()
    assert len(data) > 1000


def _db() -> Session:
    assert database.SessionLocal is not None
    return database.SessionLocal()


def _tiny_png() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (180, 28, 28)).save(buffer, format="PNG")
    return buffer.getvalue()

"""Тесты раздела «Рассылка»: нормализация, фильтры, файлы, отметки, доступ."""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy.orm import Session

import bonus.db as database
from bonus.catalog import ensure_default_segments, list_segments
from bonus.export_mail import (
    ExportFilters,
    build_preview,
    build_workbook_bytes,
    build_zip_bytes,
    group_rows,
    load_export_rows,
    mark_delivered,
    normalize_place_key,
    parse_export_filters,
    unmark_delivered,
)
from bonus.models import Base, CertificateStatus, Role, User
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate
from bonus.values import today_msk


@pytest.fixture(autouse=True)
def fresh_db() -> None:
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)


def test_normalize_place_key_merges_variants() -> None:
    assert normalize_place_key("УК") == normalize_place_key("ук")
    assert normalize_place_key("Ук ") == normalize_place_key("ук")
    assert normalize_place_key("Жарь птица") == normalize_place_key("ЖАРЬ-ПТИЦА")
    assert normalize_place_key("МС") == normalize_place_key("М-С")
    assert normalize_place_key("ёлка") == normalize_place_key("елка")


def test_card_status_filters_expiry_balance_and_perpetual() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    ensure_default_segments(db)
    segment = list_segments(db)[0]
    today = today_msk()

    perpetual = issue_certificate(
        db,
        _draft("Бессрочный", company="УК", department="А", segment_id=segment.id, expires=None),
        hr,
        sync_external=False,
    )
    today_ok = issue_certificate(
        db,
        _draft("Сегодня", company="УК", department="А", segment_id=segment.id, expires=today),
        hr,
        sync_external=False,
    )
    expired = issue_certificate(
        db,
        _draft(
            "Просрочен",
            company="УК",
            department="Б",
            segment_id=segment.id,
            expires=today - timedelta(days=1),
        ),
        hr,
        sync_external=False,
    )
    zero = issue_certificate(
        db,
        _draft(
            "Ноль",
            company="УК",
            department="В",
            segment_id=segment.id,
            expires=None,
            balance=0,
            nominal=100_000,
        ),
        hr,
        sync_external=False,
    )
    assert zero.certificate.balance == 0
    assert expired.certificate.expires_on == today - timedelta(days=1)

    filters = ExportFilters(card_status="active", delivery="all", group_by="none")
    rows = load_export_rows(db, filters, base_url="https://example.test")
    ids = {row.certificate_id for row in rows}
    assert perpetual.certificate.id in ids
    assert today_ok.certificate.id in ids
    assert expired.certificate.id not in ids
    assert zero.certificate.id not in ids


def test_preview_and_files_share_same_cards_and_zip_group_count() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    ensure_default_segments(db)
    segment = list_segments(db)[0]
    issue_certificate(
        db,
        _draft("Иванов", company="УК", department="Жарь птица", segment_id=segment.id),
        hr,
        sync_external=False,
    )
    issue_certificate(
        db,
        _draft("Петров", company="УК", department="ЖАРЬ-ПТИЦА", segment_id=segment.id),
        hr,
        sync_external=False,
    )
    issue_certificate(
        db,
        _draft("Сидоров", company="УК", department="МС", segment_id=segment.id),
        hr,
        sync_external=False,
    )

    filters = ExportFilters(card_status="active", delivery="all", group_by="restaurant")
    rows = load_export_rows(db, filters, base_url="https://example.test")
    preview = build_preview(rows, filters)
    groups = group_rows(rows, "restaurant")
    assert preview.total == 3
    assert len(groups) == 2

    zip_bytes = build_zip_bytes(groups, base_url="https://example.test", template="")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = [name for name in archive.namelist() if name.endswith(".xlsx")]
        assert len(names) == len(groups)

    book_bytes = build_workbook_bytes(groups, base_url="https://example.test", template="")
    book = load_workbook(io.BytesIO(book_bytes))
    assert "Сводка" in book.sheetnames
    preview_ids = {row.certificate_id for row in preview.rows}
    file_ids = {row.certificate_id for group in groups for row in group.rows}
    assert preview_ids == file_ids


def test_undelivered_filter_respects_marks() -> None:
    db = _db()
    hr = _person(db, "hr", Role.hr)
    ensure_default_segments(db)
    segment = list_segments(db)[0]
    first = issue_certificate(
        db,
        _draft("Один", company="УК", department="А", segment_id=segment.id),
        hr,
        sync_external=False,
    )
    second = issue_certificate(
        db,
        _draft("Два", company="УК", department="Б", segment_id=segment.id),
        hr,
        sync_external=False,
    )
    mark_delivered(db, [first.certificate.id], actor=hr, group_label="А")

    undelivered = load_export_rows(
        db,
        ExportFilters(card_status="active", delivery="undelivered"),
        base_url="https://example.test",
    )
    delivered = load_export_rows(
        db,
        ExportFilters(card_status="active", delivery="delivered"),
        base_url="https://example.test",
    )
    assert {row.certificate_id for row in undelivered} == {second.certificate.id}
    assert {row.certificate_id for row in delivered} == {first.certificate.id}

    unmark_delivered(db, [first.certificate.id])
    again = load_export_rows(
        db,
        ExportFilters(card_status="active", delivery="undelivered"),
        base_url="https://example.test",
    )
    assert {row.certificate_id for row in again} == {
        first.certificate.id,
        second.certificate.id,
    }


def test_export_forbidden_for_manager(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'export.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SEED_DEMO", "true")
    from bonus.config import get_settings

    get_settings.cache_clear()
    import bonus.main as main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        page = client.get("/login")
        token = _csrf(page.text)
        client.post(
            "/login",
            data={
                "login": "manager",
                "password": "mugle-desk",
                "csrf_token": token,
                "next": "/",
            },
            follow_redirects=True,
        )
        response = client.get("/hr/export", follow_redirects=False)
        assert response.status_code in {302, 303, 401, 403}
        if response.status_code in {302, 303}:
            assert "/hr/export" not in (response.headers.get("location") or "")


def test_parse_filters_defaults() -> None:
    filters = parse_export_filters()
    assert filters.card_status == "active"
    assert filters.delivery == "undelivered"
    assert filters.group_by == "restaurant"


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


def _draft(
    full_name: str,
    *,
    company: str,
    department: str,
    segment_id: int,
    expires: date | None = date(2030, 1, 1),
    balance: int | None = None,
    nominal: int = 100_000,
) -> IssueDraft:
    return IssueDraft(
        full_name=full_name,
        login="",
        password="",
        nominal=nominal,
        balance=balance,
        note="",
        expires_on=expires,
        segment_id=segment_id,
        segment_name="тест",
        company=company,
        department=department,
        job_title="официант",
    )


def _csrf(html: str) -> str:
    import re

    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert found is not None
    return found.group(1)

"""Быстрая сводка HR: паритет со старой реализацией и бюджет SQL."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

import bonus.db as database
from bonus.catalog import create_segment, save_template
from bonus.config import get_settings
from bonus.models import AccrualPeriod, Base, Certificate, CertificateStatus, Role, User
from bonus.parts import PartDraft
from bonus.present import search_certificate_rows
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate
from bonus.values import today_msk

# Эталон старой реализации — только для паритета / бенча (не в пакете bonus).
sys_path_root = Path(__file__).resolve().parents[1]
import sys

if str(sys_path_root) not in sys.path:
    sys.path.insert(0, str(sys_path_root))
from scripts.legacy_summary_search import search_certificates_full  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    database.configure_database("sqlite://")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    from bonus.schema import ensure_schema

    ensure_schema(database.engine)
    monkeypatch.setattr(
        "bonus.security.hash_password",
        lambda password: f"fast:{password}",
    )
    monkeypatch.setattr(
        "bonus.services.hash_password",
        lambda password: f"fast:{password}",
    )
    monkeypatch.setattr("bonus.present.barcode_svg", lambda _code: "<svg/>")
    monkeypatch.setattr(
        "bonus.services._start_issue_external_sync",
        lambda _certificate_id: None,
    )
    monkeypatch.setattr(
        "bonus.sheets_queue.enqueue_certificate_ids",
        lambda _db, _ids: 0,
    )
    yield
    get_settings.cache_clear()


def test_summary_rows_match_full_implementation() -> None:
    db = _db()
    _seed(db, cards=60)
    segment_id = _first_segment_id(db)
    # Покрываем sort × status × сегмент × запрос × offset, без полного декартова
    # произведения (старый full CardView слишком тяжёлый на тысячах прогонов).
    cases = [
        ("name", "all", None, "", 0, 30),
        ("name", "all", None, "иван", 0, 30),
        ("name", "all", None, "ИВАН", 0, 30),
        ("name", "active", segment_id, "", 0, 30),
        ("name", "expired", None, "", 0, 15),
        ("name", "blocked", None, "", 0, 15),
        ("balance", "all", None, "", 0, 30),
        ("balance", "all", None, "7999", 0, 10),
        ("expires", "all", None, "", 0, 30),
        ("expires", "active", None, "MR-", 10, 15),
        ("name", "all", None, "Ланч", 0, 30),
        ("name", "all", None, "", 10, 15),
        ("name", "all", None, "", 0, 5),
    ]
    for sort, status, seg, query, offset, limit in cases:
        sid = seg if seg and seg > 0 else None
        old = search_certificates_full(
            db, query, sid, sort=sort, status=status, offset=offset, limit=limit
        )
        new = search_certificate_rows(
            db, query, sid, sort=sort, status=status, offset=offset, limit=limit
        )
        assert new.total == old.total, (sort, status, query, offset)
        assert [card.id for card in new.cards] == [card.id for card in old.cards]
        assert new.has_more == old.has_more
        for left, right in zip(new.cards, old.cards, strict=True):
            assert left.balance == right.balance
            assert left.segment_name == right.segment_name
            assert left.status == right.status
            assert left.single_use == right.single_use


def test_summary_query_budget_independent_of_table_size() -> None:
    db = _db()
    _seed_fast(db, cards=300)
    engine = database.engine
    assert engine is not None
    counter = {"n": 0}

    def _count(_conn, _cursor, _statement, _parameters, _context, _executemany) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        counter["n"] = 0
        page = search_certificate_rows(db, "", None, offset=0, limit=30)
        assert len(page.cards) == 30
        used = counter["n"]
        assert used <= 6, used
        counter["n"] = 0
        page2 = search_certificate_rows(db, "иван", None, offset=0, limit=30)
        assert page2.total >= 0
        assert counter["n"] <= 6, counter["n"]
    finally:
        event.remove(engine, "before_cursor_execute", _count)


def test_cyrillic_case_insensitive_search_on_sqlite() -> None:
    db = _db()
    _seed(db, cards=5, force_name="Иванов Иван")
    page = search_certificate_rows(db, "иван", None)
    assert page.total >= 1
    assert any("иван" in card.holder_name.casefold() for card in page.cards)


def test_like_special_chars_are_safe() -> None:
    db = _db()
    _seed(db, cards=5)
    for query in ("%", "_", "'", "100%"):
        page = search_certificate_rows(db, query, None)
        assert page.total == 0 or all(
            query.casefold() not in card.holder_name.casefold()
            or query in {"%", "_", "'"}
            for card in page.cards
        )
        # %, _ не должны матчить всех через LIKE без escape
        if query in {"%", "_"}:
            assert page.total == 0


def _db():
    assert database.SessionLocal is not None
    return database.SessionLocal()


def _first_segment_id(db) -> int:
    from bonus.catalog import list_segments

    items = list_segments(db)
    assert items
    return items[0].id


def _person(db, login: str, role: Role) -> User:
    user = User(
        full_name="HR Test",
        login=login,
        password_hash=hash_password("secret123"),
        role=role,
        restaurant="",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _tiny_png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 168, 168)).save(buffer, format="PNG")
    return buffer.getvalue()


def _seed_fast(db, *, cards: int) -> None:
    """Массовая вставка без issue_certificate — только для бюджета SQL."""
    from datetime import datetime, timezone

    segment = create_segment(db, "Карта сотрудника", AccrualPeriod.monthly)
    now = datetime.now(timezone.utc)
    for index in range(cards):
        holder = User(
            full_name=f"Иванов {index}" if index % 5 == 0 else f"Person {index}",
            login=f"u{index}",
            password_hash="fast:x",
            role=Role.employee,
            restaurant="",
            department="Офис",
            job_title="Сотрудник",
            phone=f"+7000{index:06d}",
            is_active=True,
        )
        db.add(holder)
        db.flush()
        cert = Certificate(
            public_number=f"MR-{index + 1:05d}",
            token=f"tok{index:08d}",
            short_code=f"{index:08d}",
            holder_id=holder.id,
            segment_id=segment.id,
            template_id=None,
            color="#00A8A8",
            discount_percent=0,
            single_use=False,
            use_days="1234567",
            accrual_days="12345",
            nominal=1000_00,
            balance=1000_00,
            note="",
            ledger_sheet="employee",
            company="МУГЛЕ",
            status=CertificateStatus.active,
            expires_on=None,
            is_deleted=False,
            created_at=now,
        )
        db.add(cert)
    db.commit()


def _seed(db, *, cards: int, force_name: str | None = None) -> User:
    hr = _person(db, "hr-summary", Role.hr)
    employee = create_segment(db, "Карта сотрудника", AccrualPeriod.monthly)
    lunch = create_segment(db, "Ланч", AccrualPeriod.daily, accrual_days="12345")
    png = _tiny_png()
    template = save_template(
        db,
        name="Карта сотрудника",
        filename="e.png",
        payload=png,
        content_type="image/png",
        segment_id=employee.id,
    )
    combo = save_template(
        db,
        name="Комбо карта",
        filename="c.png",
        payload=png,
        content_type="image/png",
        segment_id=employee.id,
        segment2_id=lunch.id,
    )
    today = today_msk()
    for index in range(cards):
        name = force_name or (
            "Иванов Иван"
            if index % 7 == 0
            else f"Сотрудник {index} Петров"
            if index % 2 == 0
            else f"Employee {index} Smith"
        )
        status = CertificateStatus.active
        expires: date | None = today + timedelta(days=30)
        if index % 11 == 0:
            status = CertificateStatus.blocked
        elif index % 9 == 0:
            expires = today - timedelta(days=3)
        elif index % 5 == 0:
            expires = None
        use_combo = index % 4 == 0
        parts = ()
        if use_combo:
            parts = (
                PartDraft(
                    segment_id=employee.id,
                    nominal=5000_00,
                    use_days="12345",
                    accrual_days="12345",
                    sort_order=1,
                ),
                PartDraft(
                    segment_id=lunch.id,
                    nominal=500_00,
                    use_days="12345",
                    accrual_days="12345",
                    sort_order=2,
                ),
            )
        issued = issue_certificate(
            db,
            IssueDraft(
                full_name=name,
                login="",
                password="",
                nominal=5500_00 if use_combo else 3000_00 + index,
                balance=None,
                note=f"note-{index}",
                expires_on=expires,
                segment_id=employee.id,
                template_id=combo.id if use_combo else template.id,
                color="#00A8A8",
                department="ХВВ" if index % 3 else "Офис",
                job_title="Официант" if index % 2 else "Менеджер",
                phone=f"+7999000{index:04d}",
                discount_percent=0,
                segment_name=employee.name,
                single_use=False,
                use_days="12345",
                accrual_days="12345",
                ledger_sheet="employee",
                company="МУГЛЕ",
                birth_date=None,
                show_wallet_buttons=False,
                template_name=combo.name if use_combo else template.name,
                parts=parts,
            ),
            hr,
            sync_external=False,
        )
        cert = issued.certificate
        if status == CertificateStatus.blocked:
            cert.status = CertificateStatus.blocked
            db.commit()
        if index == cards - 1:
            cert.is_deleted = True
            db.commit()
    return hr

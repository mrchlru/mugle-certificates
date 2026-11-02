"""Замер сводки HR: SQL-запросы и время на 1500 карт.

Запуск из корня репозитория:
  PYTHONIOENCODING=utf-8 python scripts/bench_search.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import event

import bonus.db as database
from bonus.catalog import create_segment, save_template
from bonus.config import get_settings
from bonus.models import AccrualPeriod, Base, Role, User
from bonus.present import dashboard_stats, search_certificate_rows
from scripts.legacy_summary_search import search_certificates_full
from bonus.security import hash_password
from bonus.services import IssueDraft, issue_certificate


def main() -> None:
    data = ROOT / "data" / "bench-search"
    data.mkdir(parents=True, exist_ok=True)
    get_settings.cache_clear()
    database.configure_database(f"sqlite:///{data / 'bench.db'}")
    assert database.engine is not None
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    from bonus.schema import ensure_schema

    ensure_schema(database.engine)

    # PBKDF2 тормозит выпуск — для бенча достаточно заглушки.
    import bonus.security as security
    import bonus.services as services

    security.hash_password = lambda password: f"fast:{password}"  # type: ignore[assignment]
    services.hash_password = lambda password: f"fast:{password}"  # type: ignore[assignment]

    db = database.SessionLocal()
    assert db is not None
    print("Сидим 1500 карт…", flush=True)
    _seed(db, 1500)
    print("Готово. Замер…", flush=True)

    rows = []
    for label, fn in (
        ("до (full CardView)", search_certificates_full),
        ("после (SQL rows)", search_certificate_rows),
    ):
        first = _measure(fn, db, "", 0)
        second = _measure(fn, db, "", 30)
        search = _measure(fn, db, "иван", 0)
        rows.append((label, first, second, search))

    dash = _measure_dashboard(db)
    print()
    print("| Режим | /hr стр.1 | /hr offset=30 | поиск «иван» |")
    print("|---|---|---|---|")
    for label, first, second, search in rows:
        print(
            f"| {label} | {first[0]:.3f}с / {first[1]} SQL | "
            f"{second[0]:.3f}с / {second[1]} SQL | "
            f"{search[0]:.3f}с / {search[1]} SQL |"
        )
    print(f"| dashboard_stats | {dash[0]:.3f}с / {dash[1]} SQL | — | — |")
    db.close()


def _measure(fn, db, query: str, offset: int) -> tuple[float, int]:
    engine = database.engine
    assert engine is not None
    counter = {"n": 0}

    def _count(*_args) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        counter["n"] = 0
        started = time.perf_counter()
        page = fn(db, query, None, offset=offset, limit=30)
        elapsed = time.perf_counter() - started
        _ = page.total, len(page.cards)
        return elapsed, counter["n"]
    finally:
        event.remove(engine, "before_cursor_execute", _count)


def _measure_dashboard(db) -> tuple[float, int]:
    engine = database.engine
    assert engine is not None
    counter = {"n": 0}

    def _count(*_args) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        counter["n"] = 0
        started = time.perf_counter()
        stats = dashboard_stats(db, None)
        elapsed = time.perf_counter() - started
        _ = stats.active_count
        return elapsed, counter["n"]
    finally:
        event.remove(engine, "before_cursor_execute", _count)


def _seed(db, cards: int) -> None:
    hr = User(
        full_name="Bench HR",
        login="bench-hr",
        password_hash=hash_password("x"),
        role=Role.hr,
        restaurant="",
        is_active=True,
    )
    db.add(hr)
    db.commit()
    db.refresh(hr)
    segment = create_segment(db, "Карта сотрудника", AccrualPeriod.monthly)
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (0, 168, 168)).save(buffer, format="PNG")
    template = save_template(
        db,
        name="Карта сотрудника",
        filename="b.png",
        payload=buffer.getvalue(),
        content_type="image/png",
        segment_id=segment.id,
    )
    for index in range(cards):
        issue_certificate(
            db,
            IssueDraft(
                full_name=f"Сотрудник {index} Иванов" if index % 5 == 0 else f"Person {index}",
                login="",
                password="",
                nominal=1000_00 + index,
                balance=None,
                note="",
                expires_on=None,
                segment_id=segment.id,
                template_id=template.id,
                color="#00A8A8",
                department="Офис",
                job_title="Сотрудник",
                phone=f"+7000{index:06d}",
                discount_percent=0,
                segment_name=segment.name,
                single_use=False,
                use_days="1234567",
                accrual_days="12345",
                ledger_sheet="employee",
                company="МУГЛЕ",
                birth_date=None,
                show_wallet_buttons=False,
                template_name=template.name,
                parts=(),
            ),
            hr,
            sync_external=False,
        )


if __name__ == "__main__":
    main()

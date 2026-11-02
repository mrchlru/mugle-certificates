"""Разбор GOOGLE_SERVICE_ACCOUNT_JSON и дедуп номеров карт в Sheets."""

from bonus.sheets_sync import (
    _dedupe_worksheet,
    _duplicate_row_indexes,
    _normalize_card_number,
    _parse_service_account_json,
)


def test_parse_compact_json() -> None:
    raw = '{"type": "service_account", "project_id": "x", "private_key": "k\\n"}'
    data = _parse_service_account_json(raw)
    assert data["type"] == "service_account"
    assert data["private_key"] == "k\n"


def test_parse_timeweb_pretty_with_literal_newlines() -> None:
    """Как в экспорте Timeweb: \\n между полями, PEM-\\n внутри строки."""
    raw = (
        '"{\\n'
        '  "type": "service_account",\\n'
        '  "project_id": "demo",\\n'
        '  "private_key": "-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n",\\n'
        '  "client_email": "a@demo.iam.gserviceaccount.com"\\n'
        '}"'
    )
    data = _parse_service_account_json(raw)
    assert data["project_id"] == "demo"
    assert data["private_key"].startswith("-----BEGIN PRIVATE KEY-----\n")
    assert "\nABC\n" in data["private_key"]


def test_normalize_card_number() -> None:
    assert _normalize_card_number("MR-00003") == "MR-00003"
    assert _normalize_card_number("mr-3") == "MR-00003"
    assert _normalize_card_number("MR00003") == "MR-00003"


def test_duplicate_row_indexes_keeps_first() -> None:
    values = [
        "Номер карты",
        "MR-00001",
        "MR-00002",
        "mr-1",
        "MR-00002",
        "MR-00003",
        "MR00001",
    ]
    assert _duplicate_row_indexes(values) == [4, 5, 7]


def test_dedupe_worksheet_deletes_from_bottom() -> None:
    class FakeSheet:
        def __init__(self) -> None:
            self.col = [
                "Номер карты",
                "MR-00010",
                "MR-00011",
                "MR-00010",
                "MR-11",
            ]
            self.deleted: list[int] = []

        def col_values(self, _index: int) -> list[str]:
            return list(self.col)

        def delete_rows(self, start: int, end: int | None = None) -> None:
            stop = end or start
            self.deleted.append(start)
            del self.col[start - 1 : stop]

    sheet = FakeSheet()
    removed = _dedupe_worksheet(sheet)
    assert removed == 2
    assert sheet.deleted == [5, 4]
    assert sheet.col == ["Номер карты", "MR-00010", "MR-00011"]

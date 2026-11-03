"""Разбор GOOGLE_SERVICE_ACCOUNT_JSON и дедуп номеров карт в Sheets."""

from bonus.sheets_sync import (
    _collapse_row_ranges,
    _dedupe_worksheet,
    _duplicate_row_indexes,
    _is_quota_error,
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


def test_collapse_row_ranges() -> None:
    assert _collapse_row_ranges([7, 4, 5, 4]) == [(4, 5), (7, 7)]
    assert _collapse_row_ranges([]) == []


def test_is_quota_error() -> None:
    assert _is_quota_error(RuntimeError("APIError: [429]: Quota exceeded"))
    assert not _is_quota_error(RuntimeError("permission denied"))


def test_dedupe_worksheet_batches_from_bottom() -> None:
    """Дубли уходят пачками через batchUpdate; первая встреча номера остаётся."""

    class FakeSpreadsheet:
        def __init__(self) -> None:
            self.batches: list[list[tuple[int, int]]] = []

        def batch_update(self, body: dict) -> None:
            chunk: list[tuple[int, int]] = []
            for request in body["requests"]:
                span = request["deleteDimension"]["range"]
                start = span["startIndex"] + 1
                end = span["endIndex"]
                chunk.append((start, end))
                del sheet.col[start - 1 : end]
            self.batches.append(chunk)

    class FakeSheet:
        def __init__(self) -> None:
            self.id = 42
            self.title = "карты сотрудников"
            self.spreadsheet = FakeSpreadsheet()
            self.col = [
                "Номер карты",
                "MR-00010",
                "MR-00011",
                "MR-00010",
                "MR-11",
                "MR-00012",
                "MR-00012",
            ]

        def col_values(self, _index: int) -> list[str]:
            return list(self.col)

    sheet = FakeSheet()
    removed = _dedupe_worksheet(sheet)
    assert removed == 3
    assert sheet.col == ["Номер карты", "MR-00010", "MR-00011", "MR-00012"]
    assert sheet.spreadsheet.batches == [[(7, 7), (4, 5)]]


def test_dedupe_worksheet_retries_quota(monkeypatch) -> None:
    """При 429 пачка повторяется после паузы, строки не теряются."""

    class FakeSpreadsheet:
        def __init__(self) -> None:
            self.calls = 0

        def batch_update(self, body: dict) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("APIError: [429]: Quota exceeded for Write requests")
            for request in body["requests"]:
                span = request["deleteDimension"]["range"]
                start = span["startIndex"] + 1
                end = span["endIndex"]
                del sheet.col[start - 1 : end]

    class FakeSheet:
        def __init__(self) -> None:
            self.id = 7
            self.title = "тест"
            self.spreadsheet = FakeSpreadsheet()
            self.col = ["Номер карты", "MR-00001", "MR-00001"]

        def col_values(self, _index: int) -> list[str]:
            return list(self.col)

    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    sheet = FakeSheet()
    removed = _dedupe_worksheet(sheet)
    assert removed == 1
    assert sheet.col == ["Номер карты", "MR-00001"]
    assert sheet.spreadsheet.calls == 2
    assert sleeps and sleeps[0] >= 20.0

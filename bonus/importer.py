"""Чтение Excel и CSV со старыми сертификатами."""

import csv
import io
from dataclasses import dataclass
from datetime import date

from openpyxl import load_workbook

from bonus.services import BonusError, IssueDraft
from bonus.values import MoneyError, parse_date, parse_money

_ALIASES: dict[str, set[str]] = {
    "full_name": {"фио", "ф.и.о.", "имя", "сотрудник", "fullname", "full_name", "name", "фио сотрудника"},
    "login": {"логин", "login", "user"},
    "password": {"пароль", "password", "pass"},
    "nominal": {"номинал", "сумма", "nominal", "выдано", "сумма сертификата", "лимит"},
    "balance": {"остаток", "баланс", "balance", "осталось"},
    "note": {"комментарий", "примечание", "note", "коммент", "ресторан", "примечание"},
    "expires": {"срок", "действует до", "expires", "дата", "годен до"},
}


@dataclass(frozen=True)
class ParsedRow:
    """Одна строка таблицы до записи в базу."""

    row_number: int
    draft: IssueDraft | None
    error: str = ""


@dataclass(frozen=True)
class ImportRowResult:
    """Чем закончилась конкретная строка файла."""

    row_number: int
    ok: bool
    message: str
    login: str = ""
    password: str = ""
    public_number: str = ""
    link: str = ""


def parse_table(filename: str, payload: bytes) -> list[ParsedRow]:
    """Разбирает xlsx или csv в черновики сертификатов."""
    if len(payload) > 5_000_000:
        raise BonusError("Файл больше 5 МБ")
    if filename.lower().endswith(".xlsx"):
        grid = _read_xlsx(payload)
    else:
        grid = _read_csv(payload)
    if not grid:
        raise BonusError("В файле нет строк")
    header, start = _header_map(grid[0])
    rows: list[ParsedRow] = []
    for offset, cells in enumerate(grid[start:], start=start + 1):
        if _row_empty(cells):
            continue
        if len(rows) >= 5000:
            raise BonusError("В файле больше 5000 строк")
        try:
            rows.append(ParsedRow(row_number=offset, draft=_draft_from_cells(cells, header)))
        except BonusError as exc:
            rows.append(ParsedRow(row_number=offset, draft=None, error=exc.message))
    if not rows:
        raise BonusError("Не нашёл ни одного человека в таблице")
    return rows


def _read_xlsx(payload: bytes) -> list[tuple[object, ...]]:
    try:
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except Exception as exc:
        raise BonusError("Не удалось открыть Excel-файл") from exc
    sheet = workbook.active
    if sheet is None:
        raise BonusError("В книге нет листа")
    return [tuple(row) for row in sheet.iter_rows(values_only=True)]


def _read_csv(payload: bytes) -> list[tuple[object, ...]]:
    text = _decode(payload)
    delimiter = ";" if text.count(";") > text.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [tuple(row) for row in reader]


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BonusError("Сохраните таблицу в UTF-8 или как Excel")


def _header_map(first: tuple[object, ...]) -> tuple[dict[str, int], int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(first):
        key = _header_key(cell)
        if key is not None:
            mapping[key] = index
    if "full_name" in mapping or "nominal" in mapping or "balance" in mapping:
        return mapping, 1
    return {
        "full_name": 0,
        "login": 1,
        "password": 2,
        "nominal": 3,
        "balance": 4,
        "note": 5,
        "expires": 6,
    }, 0


def _header_key(cell: object) -> str | None:
    title = " ".join(str(cell or "").strip().casefold().split())
    if title == "":
        return None
    for key, names in _ALIASES.items():
        if title in names:
            return key
    return None


def _draft_from_cells(cells: tuple[object, ...], header: dict[str, int]) -> IssueDraft:
    try:
        nominal = parse_money(_cell(cells, header, "nominal"), blank_as_none=True)
        balance = parse_money(_cell(cells, header, "balance"), blank_as_none=True)
        expires = parse_date(_cell(cells, header, "expires"))
    except MoneyError as exc:
        raise BonusError(exc.message) from exc
    return IssueDraft(
        full_name=str(_cell(cells, header, "full_name") or "").strip(),
        login=str(_cell(cells, header, "login") or "").strip(),
        password=str(_cell(cells, header, "password") or "").strip(),
        nominal=0 if nominal is None else nominal,
        balance=balance,
        note=str(_cell(cells, header, "note") or "").strip(),
        expires_on=expires if isinstance(expires, date) or expires is None else None,
    )


def _cell(cells: tuple[object, ...], header: dict[str, int], key: str) -> object:
    index = header.get(key)
    if index is None or index >= len(cells):
        return ""
    return cells[index]


def _row_empty(cells: tuple[object, ...]) -> bool:
    return all(str(cell or "").strip() == "" for cell in cells)

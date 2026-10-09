"""Чтение Excel и CSV со старыми сертификатами."""

import csv
import io
from dataclasses import dataclass
from datetime import date

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

from bonus.services import BonusError, IssueDraft, normalize_discount
from bonus.values import MoneyError, parse_date, parse_money

_ALIASES: dict[str, set[str]] = {
    "full_name": {"фио", "ф.и.о.", "имя", "сотрудник", "fullname", "full_name", "name", "фио сотрудника"},
    "login": {"логин", "login", "user"},
    "password": {"пароль", "password", "pass"},
    "nominal": {"номинал", "сумма", "nominal", "выдано", "сумма сертификата", "лимит"},
    "balance": {"остаток", "баланс", "balance", "осталось"},
    "note": {"комментарий", "примечание", "note", "коммент"},
    "expires": {"срок", "действует до", "expires", "дата", "годен до"},
    "department": {"отдел", "department", "подразделение"},
    "job_title": {"должность", "job_title", "позиция", "position", "роль"},
    "phone": {"телефон", "phone", "тел", "мобильный"},
    "discount": {"процент", "скидка", "discount", "%", "процент на карточке"},
    "segment": {"сегмент", "segment", "тип", "тип сертификата"},
}

_SAMPLE_HEADERS: tuple[str, ...] = (
    "ФИО",
    "Отдел",
    "Должность",
    "Телефон",
    "Номинал",
    "Процент",
    "Сегмент",
    "Комментарий",
    "Срок",
    "Остаток",
)

_SAMPLE_WIDTHS: tuple[int, ...] = (22, 14, 16, 18, 12, 10, 26, 16, 14, 12)

_SAMPLE_ROWS: tuple[tuple[object, ...], ...] = (
    (
        "Иванов Иван",
        "Офис",
        "Менеджер",
        "+7 900 111-22-33",
        10000,
        30,
        "Ежемесячные бонусы",
        "Офис",
        "31.12.2026",
        "",
    ),
    (
        "Петрова Анна",
        "Кухня",
        "Повар",
        "+7 900 222-33-44",
        5000,
        "",
        "Ланч",
        "Кухня",
        "",
        3500,
    ),
)


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


def build_sample_workbook() -> bytes:
    """Готовый Excel-пример с нормальной шириной столбцов."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "sertifikaty"
    sheet.append(list(_SAMPLE_HEADERS))
    for row in _SAMPLE_ROWS:
        sheet.append(list(row))
    for index, width in enumerate(_SAMPLE_WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


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
        "department": 1,
        "job_title": 2,
        "phone": 3,
        "nominal": 4,
        "discount": 5,
        "segment": 6,
        "note": 7,
        "expires": 8,
        "balance": 9,
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
        discount = _parse_discount(_cell(cells, header, "discount"))
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
        department=str(_cell(cells, header, "department") or "").strip(),
        job_title=str(_cell(cells, header, "job_title") or "").strip(),
        phone=str(_cell(cells, header, "phone") or "").strip(),
        discount_percent=discount,
        segment_name=str(_cell(cells, header, "segment") or "").strip(),
    )


def _parse_discount(raw: object) -> int:
    text = str(raw or "").strip().replace("%", "")
    if text == "":
        return 0
    try:
        return normalize_discount(int(float(text)))
    except (TypeError, ValueError) as exc:
        raise BonusError("Процент — целое число от 0 до 100") from exc


def _cell(cells: tuple[object, ...], header: dict[str, int], key: str) -> object:
    index = header.get(key)
    if index is None or index >= len(cells):
        return ""
    return cells[index]


def _row_empty(cells: tuple[object, ...]) -> bool:
    return all(str(cell or "").strip() == "" for cell in cells)

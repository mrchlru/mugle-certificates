"""Запись учёта сертификатов в Google Таблицу.

Не роняет выпуск/списание кассы: ошибки только в лог.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bonus.config import get_settings
from bonus.ledger import (
    LedgerSheet,
    ledger_headers,
    ledger_is_single_use,
    ledger_title,
    parse_ledger_sheet,
)
from bonus.models import Certificate, User
from bonus.values import format_date, format_money_plain, today_msk

logger = logging.getLogger("bonus.sheets")

_EXPIRED_MARK = "истек"


@dataclass(frozen=True)
class SheetUsageEntry:
    """Одно списание для колонок использования в таблице."""

    used_on: date
    amount_kopecks: int
    restaurant: str
    order_ref: str = ""


def sheets_enabled() -> bool:
    """Включена ли синхронизация (есть таблица и ключ сервисного аккаунта)."""
    settings = get_settings()
    return bool(settings.google_sheets_spreadsheet_id.strip()) and _has_credentials(settings)


def sync_certificate_issued(
    certificate: Certificate,
    holder: User,
    *,
    segment_name: str = "",
) -> None:
    """Добавляет строку карты на нужный лист сразу после выпуска (без дублей)."""
    if not sheets_enabled():
        logger.warning(
            "Google Sheets: выпуск %s не записан — нет spreadsheet id или ключа SA",
            certificate.public_number,
        )
        return
    try:
        kind = parse_ledger_sheet(certificate.ledger_sheet)
        worksheet = _worksheet(kind)
        row_index = _ensure_certificate_row(
            worksheet,
            kind,
            certificate,
            holder,
            segment_name=segment_name,
        )
        _write_balance_cell(worksheet, kind, row_index, certificate.balance)
        logger.info(
            "Google Sheets: выпуск %s → лист «%s», строка %s",
            certificate.public_number,
            worksheet.title,
            row_index,
        )
    except Exception:
        logger.exception("Google Sheets: не удалось записать выпуск %s", certificate.public_number)


def sync_certificate_usage(
    certificate: Certificate,
    holder: User,
    *,
    usages: list[SheetUsageEntry],
    segment_name: str = "",
    expired: bool = False,
) -> None:
    """Пишет строку карты и колонки использования по журналу списаний."""
    if not sheets_enabled():
        logger.warning(
            "Google Sheets: использование %s не записано — нет spreadsheet id или ключа SA",
            certificate.public_number,
        )
        return
    try:
        kind = parse_ledger_sheet(certificate.ledger_sheet)
        worksheet = _worksheet(kind)
        row_index = _ensure_certificate_row(
            worksheet,
            kind,
            certificate,
            holder,
            segment_name=segment_name,
        )
        date_col, amount_col, place_col = _usage_columns(kind)
        if expired:
            _set_cell(worksheet, row_index, date_col, _EXPIRED_MARK)
            _set_cell(worksheet, row_index, amount_col, format_money_plain(certificate.balance))
            logger.info(
                "Google Sheets: %s помечена «истек» на листе «%s»",
                certificate.public_number,
                worksheet.title,
            )
            return
        _write_usage_columns(
            worksheet,
            kind,
            row_index,
            date_col,
            amount_col,
            place_col,
            usages,
            remaining_kopecks=certificate.balance,
        )
        logger.info(
            "Google Sheets: %s — %s списаний на листе «%s», строка %s, остаток %s",
            certificate.public_number,
            len(usages),
            worksheet.title,
            row_index,
            certificate.balance,
        )
    except Exception:
        logger.exception(
            "Google Sheets: не удалось обновить использование %s",
            certificate.public_number,
        )


def _ensure_certificate_row(
    worksheet: Any,
    kind: LedgerSheet,
    certificate: Certificate,
    holder: User,
    *,
    segment_name: str,
) -> int:
    """Находит строку карты; если выпуска не было в таблице — дописывает строку."""
    row_index = _find_row(worksheet, certificate.public_number)
    if row_index is not None:
        return row_index
    row = _issue_row(kind, certificate, holder, segment_name=segment_name)
    appended = _append_row(worksheet, row)
    logger.info(
        "Google Sheets: добавлена карта %s на лист «%s», строка %s",
        certificate.public_number,
        worksheet.title,
        appended,
    )
    found = _find_row(worksheet, certificate.public_number)
    return found if found is not None else appended


def _append_row(worksheet: Any, row: list[str]) -> int:
    """Дописывает строку и возвращает её номер (1-based)."""
    response = worksheet.append_row(
        row,
        value_input_option="USER_ENTERED",
        table_range="A1",
    )
    parsed = _row_from_append_response(response)
    if parsed is not None:
        return parsed
    return max(len(worksheet.col_values(1)), 2)


def _row_from_append_response(response: Any) -> int | None:
    """Достаёт номер строки из ответа Sheets API после append."""
    if not isinstance(response, dict):
        return None
    updates = response.get("updates")
    if not isinstance(updates, dict):
        return None
    updated_range = str(updates.get("updatedRange") or "")
    if "!" not in updated_range:
        return None
    cells = updated_range.split("!", 1)[1]
    start = cells.split(":", 1)[0]
    digits = "".join(char for char in start if char.isdigit())
    return int(digits) if digits.isdigit() else None


def _write_balance_cell(
    worksheet: Any,
    kind: LedgerSheet,
    row_index: int,
    remaining_kopecks: int,
) -> None:
    """Пишет текущий остаток бонусов в колонку «Сумма» использования."""
    _, amount_col, _ = _usage_columns(kind)
    _set_cell(worksheet, row_index, amount_col, format_money_plain(remaining_kopecks))


def _write_usage_columns(
    worksheet: Any,
    kind: LedgerSheet,
    row_index: int,
    date_col: int,
    amount_col: int,
    place_col: int,
    usages: list[SheetUsageEntry],
    *,
    remaining_kopecks: int,
) -> None:
    """Перезаписывает даты, остаток и ресторан/заказ по журналу списаний."""
    if not usages:
        _set_cell(worksheet, row_index, date_col, "")
        _set_cell(worksheet, row_index, amount_col, format_money_plain(remaining_kopecks))
        _set_cell(worksheet, row_index, place_col, "")
        return
    if ledger_is_single_use(kind):
        last = usages[-1]
        _set_cell(worksheet, row_index, date_col, format_date(last.used_on))
        _set_cell(worksheet, row_index, amount_col, format_money_plain(remaining_kopecks))
        _set_cell(worksheet, row_index, place_col, _usage_place_label(last))
        return
    dates = ", ".join(format_date(item.used_on) for item in usages)
    places = ", ".join(_usage_place_label(item) for item in usages)
    _set_cell(worksheet, row_index, date_col, dates)
    _set_cell(worksheet, row_index, amount_col, format_money_plain(remaining_kopecks))
    _set_cell(worksheet, row_index, place_col, places)


def _usage_place_label(entry: SheetUsageEntry) -> str:
    """Ресторан и заказ одной фразой для ячейки подборки."""
    place = (entry.restaurant or "").strip()
    order = (entry.order_ref or "").strip()
    if place and order:
        return f"{place} ({order})"
    if place:
        return place
    if order:
        return f"заказ {order}"
    return "—"


def ensure_ledger_worksheets() -> list[str]:
    """Создаёт вкладки с заголовками, если их ещё нет. Для ручной проверки."""
    created: list[str] = []
    for kind in LedgerSheet:
        title = ledger_title(kind)
        worksheet = _worksheet(kind)
        headers = list(ledger_headers(kind))
        existing = worksheet.row_values(1)
        if existing != headers:
            if existing:
                worksheet.delete_rows(1)
            worksheet.insert_row(headers, 1)
            created.append(title)
        elif title not in created:
            created.append(title)
    return created


def _has_credentials(settings: Any) -> bool:
    """Ключ есть, если файл на диске или JSON задан в переменной окружения."""
    path = settings.google_service_account_file.strip()
    if path and Path(path).is_file():
        return True
    return bool(settings.google_service_account_json.strip())


def _client() -> Any:
    import gspread
    from google.oauth2.service_account import Credentials

    settings = get_settings()
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    info = _service_account_info(settings)
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def _service_account_info(settings: Any) -> dict[str, Any]:
    path = settings.google_service_account_file.strip()
    if path and Path(path).is_file():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    raw = settings.google_service_account_json.strip()
    if not raw:
        raise RuntimeError(
            "Нет ключа Google: задайте GOOGLE_SERVICE_ACCOUNT_JSON "
            "или положите файл по GOOGLE_SERVICE_ACCOUNT_FILE"
        )
    return _parse_service_account_json(raw)


def _parse_service_account_json(raw: str) -> dict[str, Any]:
    """Разбирает JSON ключа; терпит кавычки и \\n из панелей хостинга."""
    text = _normalize_service_account_json_text(raw)
    last_error: json.JSONDecodeError | None = None
    for candidate in (text, _unescape_env_json_whitespace(text)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(data, dict):
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON должен быть объектом JSON")
        return data
    raise RuntimeError(
        "GOOGLE_SERVICE_ACCOUNT_JSON не разбирается: в Timeweb вставьте компактный "
        "JSON одной строкой без кавычек вокруг всего значения"
    ) from last_error


def _normalize_service_account_json_text(raw: str) -> str:
    """Убирает обёртку кавычек и вырезает объект `{...}` из экспорта Timeweb."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _unescape_env_json_whitespace(text: str) -> str:
    """Превращает \\n/\\t вне строк JSON в пробелы; внутри строк (PEM) не трогает."""
    out: list[str] = []
    in_string = False
    escape = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(text) and text[index + 1] in "ntr":
            out.append({"n": "\n", "t": "\t", "r": "\r"}[text[index + 1]])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _spreadsheet() -> Any:
    settings = get_settings()
    return _client().open_by_key(settings.google_sheets_spreadsheet_id.strip())


def _worksheet(kind: LedgerSheet) -> Any:
    title = ledger_title(kind)
    book = _spreadsheet()
    worksheet = _find_worksheet(book, title)
    if worksheet is None:
        worksheet = book.add_worksheet(title=title, rows=2000, cols=len(ledger_headers(kind)))
        worksheet.append_row(list(ledger_headers(kind)), value_input_option="USER_ENTERED")
        return worksheet
    headers = list(ledger_headers(kind))
    current = worksheet.row_values(1)
    if not current:
        worksheet.append_row(headers, value_input_option="USER_ENTERED")
    elif current[: len(headers)] != headers:
        # Не затираем чужие данные — только если лист пустой по смыслу.
        if len(current) == 0:
            worksheet.update("A1", [headers])
    return worksheet


def _find_worksheet(book: Any, title: str) -> Any | None:
    """Ищет вкладку по имени без учёта регистра; при дублях берёт самую заполненную."""
    needle = title.casefold()
    exact: Any | None = None
    folded: list[Any] = []
    for worksheet in book.worksheets():
        current = (worksheet.title or "").strip()
        if current == title:
            exact = worksheet
            break
        if current.casefold() == needle:
            folded.append(worksheet)
    if exact is not None:
        return exact
    if not folded:
        return None
    if len(folded) == 1:
        return folded[0]
    return max(folded, key=_worksheet_data_rows)


def _worksheet_data_rows(worksheet: Any) -> int:
    """Сколько непустых строк в колонке A (кроме заголовка)."""
    values = worksheet.col_values(1)
    return sum(1 for index, value in enumerate(values) if index > 0 and str(value).strip())


def _issue_row(
    kind: LedgerSheet,
    certificate: Certificate,
    holder: User,
    *,
    segment_name: str,
) -> list[str]:
    issued = _format_dt(certificate.created_at)
    expires = format_date(certificate.expires_on) if certificate.expires_on else ""
    nominal = format_money_plain(certificate.nominal)
    company = (certificate.company or "").strip()
    place = (holder.department or "").strip()
    job = (holder.job_title or "").strip()
    birth = format_date(certificate.birth_date) if certificate.birth_date else ""
    note = (certificate.note or "").strip()
    reason = segment_name or ""
    number = certificate.public_number

    remaining = format_money_plain(certificate.balance)
    if kind in {LedgerSheet.birthday, LedgerSheet.gift_birthday}:
        return [
            number,
            holder.full_name,
            company,
            place,
            job,
            birth,
            nominal,
            "",
            remaining,
            "",
            note,
        ]
    return [
        number,
        issued,
        holder.full_name,
        company,
        place,
        job,
        reason,
        expires,
        nominal,
        "",
        remaining,
        "",
        note,
    ]


def _usage_columns(kind: LedgerSheet) -> tuple[int, int, int]:
    """1-based индексы колонок дата / сумма / ресторан использования."""
    headers = ledger_headers(kind)
    date_col = headers.index("Дата Использования") + 1
    amount_col = _last_index(headers, "Сумма") + 1
    place_col = headers.index("Ресторан") + 1
    return date_col, amount_col, place_col


def _last_index(items: tuple[str, ...], name: str) -> int:
    for index in range(len(items) - 1, -1, -1):
        if items[index] == name:
            return index
    raise ValueError(name)


def _find_row(worksheet: Any, public_number: str) -> int | None:
    values = worksheet.col_values(1)
    needle = _normalize_card_number(public_number)
    for index, value in enumerate(values, start=1):
        if index == 1:
            continue
        if _normalize_card_number(str(value)) == needle:
            return index
    return None


def _normalize_card_number(value: str) -> str:
    """Сводит номер карты к виду MR-00003 для сравнения."""
    compact = "".join(ch for ch in (value or "").upper() if ch.isalnum())
    if not compact.startswith("MR"):
        return (value or "").strip().upper()
    digits = compact[2:]
    if digits.isdigit():
        return f"MR-{int(digits):05d}"
    return compact


def _set_cell(worksheet: Any, row: int, col: int, value: str) -> None:
    worksheet.update_cell(row, col, value)


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return format_date(today_msk())
    local = value
    if local.tzinfo is not None:
        from bonus.values import MSK

        local = local.astimezone(MSK)
    return format_date(local.date())

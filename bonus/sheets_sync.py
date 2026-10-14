"""Запись учёта сертификатов в Google Таблицу.

Не роняет выпуск/списание кассы: ошибки только в лог.
"""

from __future__ import annotations

import json
import logging
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
    """Добавляет строку карты на нужный лист после выпуска."""
    if not sheets_enabled():
        return
    try:
        kind = parse_ledger_sheet(certificate.ledger_sheet)
        worksheet = _worksheet(kind)
        row = _issue_row(kind, certificate, holder, segment_name=segment_name)
        worksheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception:
        logger.exception("Google Sheets: не удалось записать выпуск %s", certificate.public_number)


def sync_certificate_usage(
    certificate: Certificate,
    *,
    used_on: date | None,
    amount_kopecks: int,
    restaurant: str,
    expired: bool = False,
) -> None:
    """Дописывает использование или помечает «истек» в строке карты."""
    if not sheets_enabled():
        return
    try:
        kind = parse_ledger_sheet(certificate.ledger_sheet)
        worksheet = _worksheet(kind)
        row_index = _find_row(worksheet, certificate.public_number)
        if row_index is None:
            logger.warning(
                "Google Sheets: карта %s не найдена на листе %s",
                certificate.public_number,
                ledger_title(kind),
            )
            return
        date_col, amount_col, place_col = _usage_columns(kind)
        if expired:
            _set_cell(worksheet, row_index, date_col, _EXPIRED_MARK)
            return
        used = used_on or today_msk()
        date_text = format_date(used)
        amount_text = format_money_plain(amount_kopecks)
        place = (restaurant or "").strip() or "—"
        if ledger_is_single_use(kind):
            _set_cell(worksheet, row_index, date_col, date_text)
            _set_cell(worksheet, row_index, amount_col, amount_text)
            _set_cell(worksheet, row_index, place_col, place)
            return
        _append_list_cell(worksheet, row_index, date_col, date_text)
        _append_list_cell(worksheet, row_index, amount_col, amount_text)
        _append_list_cell(worksheet, row_index, place_col, place)
    except Exception:
        logger.exception(
            "Google Sheets: не удалось обновить использование %s",
            certificate.public_number,
        )


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
    if settings.google_service_account_file.strip():
        return Path(settings.google_service_account_file).is_file()
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
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    raw = settings.google_service_account_json.strip()
    if not raw:
        raise RuntimeError("Нет GOOGLE_SERVICE_ACCOUNT_JSON или GOOGLE_SERVICE_ACCOUNT_FILE")
    return _parse_service_account_json(raw)


def _parse_service_account_json(raw: str) -> dict[str, Any]:
    """Разбирает JSON ключа; терпит кавычки и \\n из панелей хостинга."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON повреждён: вставьте ключ одной строкой "
            "без лишних кавычек вокруг всего JSON"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON должен быть объектом JSON")
    return data


def _spreadsheet() -> Any:
    settings = get_settings()
    return _client().open_by_key(settings.google_sheets_spreadsheet_id.strip())


def _worksheet(kind: LedgerSheet) -> Any:
    title = ledger_title(kind)
    book = _spreadsheet()
    try:
        worksheet = book.worksheet(title)
    except Exception:
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
            "",
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
        "",
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
    needle = public_number.strip()
    for index, value in enumerate(values, start=1):
        if index == 1:
            continue
        if str(value).strip() == needle:
            return index
    return None


def _set_cell(worksheet: Any, row: int, col: int, value: str) -> None:
    worksheet.update_cell(row, col, value)


def _append_list_cell(worksheet: Any, row: int, col: int, value: str) -> None:
    current = str(worksheet.cell(row, col).value or "").strip()
    if current == "" or current.casefold() == _EXPIRED_MARK:
        _set_cell(worksheet, row, col, value)
        return
    if value in {part.strip() for part in current.split(",")}:
        return
    _set_cell(worksheet, row, col, f"{current}, {value}")


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return format_date(today_msk())
    local = value
    if local.tzinfo is not None:
        from bonus.values import MSK

        local = local.astimezone(MSK)
    return format_date(local.date())

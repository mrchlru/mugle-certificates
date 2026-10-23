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
_ROW_ALERT_COLOR = {"red": 0.96, "green": 0.80, "blue": 0.80}
_ROW_CLEAR_COLOR = {"red": 1.0, "green": 1.0, "blue": 1.0}


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
) -> bool:
    """Добавляет строку карты на нужный лист сразу после выпуска (без дублей)."""
    if not sheets_enabled():
        logger.warning(
            "Google Sheets: выпуск %s не записан — нет spreadsheet id или ключа SA",
            certificate.public_number,
        )
        return False
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
        _write_profile_columns(
            worksheet,
            kind,
            row_index,
            certificate,
            holder,
            segment_name=segment_name,
        )
        _paint_certificate_row(
            worksheet,
            kind,
            row_index,
            certificate,
            usages=[],
            expired=False,
        )
        logger.info(
            "Google Sheets: выпуск %s → лист «%s», строка %s",
            certificate.public_number,
            worksheet.title,
            row_index,
        )
        return True
    except Exception as exc:
        logger.exception(
            "Google Sheets: не удалось записать выпуск %s (%s). %s",
            certificate.public_number,
            exc,
            _permission_hint() if _is_permission_error(exc) else "",
        )
        return False


@dataclass(frozen=True)
class SheetIssuedItem:
    """Один выпуск для пакетной записи в Google Sheets."""

    certificate: Certificate
    holder: User
    segment_name: str = ""


@dataclass(frozen=True)
class SheetBatchResult:
    """Итог пакетной синхронизации выпусков."""

    written: int = 0
    skipped: int = 0
    errors: int = 0
    ok_ids: tuple[int, ...] = ()
    failed_ids: tuple[int, ...] = ()


def sync_certificates_issued_batch(items: list[SheetIssuedItem]) -> SheetBatchResult:
    """Пишет пачку выпусков пакетами append_rows — без поштучных update_cell.

    Нужна при импорте: поштучная запись упирается в квоту Sheets API.
    Уже существующие номера на листе пропускает.
    """
    if not items:
        return SheetBatchResult()
    if not sheets_enabled():
        logger.warning(
            "Google Sheets: пакет из %s карт не записан — нет spreadsheet id или ключа SA",
            len(items),
        )
        return SheetBatchResult(
            errors=len(items),
            failed_ids=tuple(item.certificate.id for item in items),
        )
    written = 0
    skipped = 0
    errors = 0
    ok_ids: list[int] = []
    failed_ids: list[int] = []
    grouped: dict[LedgerSheet, list[SheetIssuedItem]] = {}
    for item in items:
        try:
            kind = parse_ledger_sheet(item.certificate.ledger_sheet)
        except Exception:
            errors += 1
            failed_ids.append(item.certificate.id)
            logger.exception(
                "Google Sheets: неизвестный лист учёта у %s",
                item.certificate.public_number,
            )
            continue
        grouped.setdefault(kind, []).append(item)
    for kind, group in grouped.items():
        try:
            ok, skip, done_ids = _append_issued_group(kind, group)
            written += ok
            skipped += skip
            ok_ids.extend(done_ids)
        except Exception as exc:
            errors += len(group)
            failed_ids.extend(item.certificate.id for item in group)
            logger.exception(
                "Google Sheets: пакет на лист «%s» не записан (%s). %s",
                ledger_title(kind),
                exc,
                _permission_hint() if _is_permission_error(exc) else "",
            )
    logger.info(
        "Google Sheets: пакет — записано %s, уже были %s, ошибок %s",
        written,
        skipped,
        errors,
    )
    return SheetBatchResult(
        written=written,
        skipped=skipped,
        errors=errors,
        ok_ids=tuple(ok_ids),
        failed_ids=tuple(failed_ids),
    )


def _append_issued_group(
    kind: LedgerSheet,
    group: list[SheetIssuedItem],
) -> tuple[int, int, list[int]]:
    """Дописывает карты одного листа пачками; возвращает (written, skipped, ok_ids)."""
    import time

    worksheet = _worksheet(kind)
    existing = {
        _normalize_card_number(str(value))
        for index, value in enumerate(worksheet.col_values(1), start=1)
        if index > 1 and str(value).strip()
    }
    rows: list[list[str]] = []
    skipped = 0
    ok_ids: list[int] = []
    for item in group:
        number = _normalize_card_number(item.certificate.public_number)
        if number in existing:
            skipped += 1
            ok_ids.append(item.certificate.id)
            continue
        rows.append(
            _issue_row(
                kind,
                item.certificate,
                item.holder,
                segment_name=item.segment_name,
            )
        )
        existing.add(number)
        ok_ids.append(item.certificate.id)
    written = 0
    chunk_size = 40
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        if not chunk:
            continue
        worksheet.append_rows(chunk, value_input_option="USER_ENTERED", table_range="A1")
        written += len(chunk)
        if start + chunk_size < len(rows):
            time.sleep(2.0)
    return written, skipped, ok_ids


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
        _write_profile_columns(
            worksheet,
            kind,
            row_index,
            certificate,
            holder,
            segment_name=segment_name,
        )
        date_col, amount_col, place_col = _usage_columns(kind)
        if expired:
            _set_cell(worksheet, row_index, date_col, _EXPIRED_MARK)
            _set_cell(worksheet, row_index, amount_col, format_money_plain(certificate.balance))
            _paint_certificate_row(
                worksheet,
                kind,
                row_index,
                certificate,
                usages=usages,
                expired=True,
            )
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
        _paint_certificate_row(
            worksheet,
            kind,
            row_index,
            certificate,
            usages=usages,
            expired=False,
        )
        logger.info(
            "Google Sheets: %s — %s списаний на листе «%s», строка %s, остаток %s",
            certificate.public_number,
            len(usages),
            worksheet.title,
            row_index,
            certificate.balance,
        )
    except Exception as exc:
        logger.exception(
            "Google Sheets: не удалось обновить использование %s (%s). %s",
            certificate.public_number,
            exc,
            _permission_hint() if _is_permission_error(exc) else "",
        )


def _is_permission_error(exc: BaseException) -> bool:
    """Google ответил 403 / нет прав на таблицу."""
    text = str(exc).casefold()
    return "403" in text or "permission" in text or "не удалось создать вкладку" in text


def delete_certificate_row(public_number: str, ledger_sheet: str) -> None:
    """Удаляет строку карты из Google Sheets целиком."""
    if not sheets_enabled():
        logger.warning(
            "Google Sheets: удаление %s пропущено — нет spreadsheet id или ключа SA",
            public_number,
        )
        return
    try:
        kind = parse_ledger_sheet(ledger_sheet)
        worksheet = _worksheet(kind)
        row_index = _find_row(worksheet, public_number)
        if row_index is None:
            logger.info(
                "Google Sheets: строка %s уже отсутствует на листе «%s»",
                public_number,
                worksheet.title,
            )
            return
        worksheet.delete_rows(row_index)
        logger.info(
            "Google Sheets: удалена строка %s с листа «%s»",
            public_number,
            worksheet.title,
        )
    except Exception as exc:
        logger.exception(
            "Google Sheets: не удалось удалить %s (%s). %s",
            public_number,
            exc,
            _permission_hint() if _is_permission_error(exc) else "",
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


def _paint_certificate_row(
    worksheet: Any,
    kind: LedgerSheet,
    row_index: int,
    certificate: Certificate,
    *,
    usages: list[SheetUsageEntry],
    expired: bool,
) -> None:
    """Красит строку красным, если карта использована, закончилась или истекла."""
    alert = _certificate_row_alert(certificate, usages, expired)
    last_col = _column_letter(len(ledger_headers(kind)))
    a1 = f"A{row_index}:{last_col}{row_index}"
    color = _ROW_ALERT_COLOR if alert else _ROW_CLEAR_COLOR
    worksheet.format(a1, {"backgroundColor": color})


def _certificate_row_alert(
    certificate: Certificate,
    usages: list[SheetUsageEntry],
    expired: bool,
) -> bool:
    """Нужно ли подсветить строку как закрытую / использованную."""
    if expired:
        return True
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return True
    if usages:
        return True
    if certificate.balance <= 0:
        return True
    return False


def _column_letter(index: int) -> str:
    """1 → A, 13 → M."""
    if index <= 0:
        return "A"
    letters = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _write_profile_columns(
    worksheet: Any,
    kind: LedgerSheet,
    row_index: int,
    certificate: Certificate,
    holder: User,
    *,
    segment_name: str,
) -> None:
    """Обновляет поля карты в строке (ФИО, срок, номинал, остаток и т.д.)."""
    headers = ledger_headers(kind)
    expires = format_date(certificate.expires_on) if certificate.expires_on else ""
    birth = format_date(certificate.birth_date) if certificate.birth_date else ""
    reason = (segment_name or "").strip()
    values: dict[str, str] = {
        "ФИО Сотрудника": holder.full_name.strip(),
        "ФИО": holder.full_name.strip(),
        "ООО": (certificate.company or "").strip(),
        "Ресторан/офис": (holder.department or "").strip(),
        "Должность": (holder.job_title or "").strip(),
        "повод/бартер": reason,
        "повод": reason,
        "сегмент": reason,
        "срок действия до": expires,
        "Дата рождения": birth,
        "Сумма сертификата": format_money_plain(certificate.nominal),
        "Комментарии": (certificate.note or "").strip(),
    }
    for index, header in enumerate(headers, start=1):
        if header not in values:
            continue
        _set_cell(worksheet, row_index, index, values[header])
    _write_balance_cell(worksheet, kind, row_index, certificate.balance)


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
    """Ключ есть, если JSON в env или файл на диске."""
    if settings.google_service_account_json.strip():
        return True
    path = settings.google_service_account_file.strip()
    return bool(path) and Path(path).is_file()


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
    """JSON из env важнее файла: на Timeweb файла обычно нет, а путь в env может быть битым."""
    raw = settings.google_service_account_json.strip()
    if raw:
        return _parse_service_account_json(raw)
    path = settings.google_service_account_file.strip()
    if path and Path(path).is_file():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    raise RuntimeError(
        "Нет ключа Google: задайте GOOGLE_SERVICE_ACCOUNT_JSON "
        "или положите файл по GOOGLE_SERVICE_ACCOUNT_FILE"
    )


def log_sheets_status() -> None:
    """Пишет в лог старта, каким SA и какими листами реально пользуемся."""
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id.strip():
        print("Teams Bonus: Google Sheets выключен — нет GOOGLE_SHEETS_SPREADSHEET_ID", flush=True)
        return
    if not _has_credentials(settings):
        print(
            "Teams Bonus: Google Sheets выключен — нет GOOGLE_SERVICE_ACCOUNT_JSON / FILE",
            flush=True,
        )
        return
    try:
        info = _service_account_info(settings)
        email = str(info.get("client_email") or "").strip()
        book = _spreadsheet()
        titles = [str(ws.title or "").strip() for ws in book.worksheets()]
        missing = [
            ledger_title(kind)
            for kind in LedgerSheet
            if _find_worksheet(book, ledger_title(kind)) is None
        ]
        print(
            f"Teams Bonus: Google Sheets SA={email} "
            f"id={settings.google_sheets_spreadsheet_id.strip()} "
            f"«{book.title}» листы={titles}",
            flush=True,
        )
        if missing:
            print(
                f"Teams Bonus: Google Sheets нет вкладок: {missing}",
                flush=True,
            )
        else:
            probe = _find_worksheet(book, ledger_title(LedgerSheet.employee))
            if probe is not None:
                probe.update_cell(1, 1, probe.cell(1, 1).value)
            print("Teams Bonus: Google Sheets запись OK", flush=True)
    except Exception as exc:
        print(
            f"Teams Bonus: Google Sheets проверка не удалась: {exc}. {_permission_hint()}",
            flush=True,
        )


def sheets_credentials_error() -> str:
    """Пусто, если ключ SA читается; иначе текст ошибки для UI/логов."""
    settings = get_settings()
    if not settings.google_sheets_spreadsheet_id.strip():
        return "Не задан GOOGLE_SHEETS_SPREADSHEET_ID"
    if not _has_credentials(settings):
        return "Не задан GOOGLE_SERVICE_ACCOUNT_JSON / FILE"
    try:
        _service_account_info(settings)
    except Exception as exc:
        return str(exc)
    return ""


def _parse_service_account_json(raw: str) -> dict[str, Any]:
    """Разбирает JSON ключа; терпит кавычки, сырые переносы PEM и \\n из панелей."""
    text = _normalize_service_account_json_text(raw)
    last_error: json.JSONDecodeError | None = None
    candidates = (
        text,
        _unescape_env_json_whitespace(text),
        _escape_raw_controls_in_json_strings(text),
        _escape_raw_controls_in_json_strings(_unescape_env_json_whitespace(text)),
    )
    for candidate in candidates:
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


def _escape_raw_controls_in_json_strings(text: str) -> str:
    """Экранирует сырые \\n/\\r/\\t внутри JSON-строк (Timeweb ломает private_key)."""
    out: list[str] = []
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                out.append(char)
                escape = False
                continue
            if char == "\\":
                out.append(char)
                escape = True
                continue
            if char == '"':
                out.append(char)
                in_string = False
                continue
            if char == "\n":
                out.append("\\n")
                continue
            if char == "\r":
                out.append("\\r")
                continue
            if char == "\t":
                out.append("\\t")
                continue
            if ord(char) < 32:
                out.append(f"\\u{ord(char):04x}")
                continue
            out.append(char)
            continue
        if char == '"':
            in_string = True
        out.append(char)
    return "".join(out)


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
        existing = _worksheet_titles(book)
        if existing:
            raise RuntimeError(
                f"Лист «{title}» не найден. В таблице есть: {existing}. "
                "Переименуйте вкладку или выберите другой лист учёта при выпуске. "
                f"{_permission_hint()}"
            )
        try:
            worksheet = book.add_worksheet(
                title=title,
                rows=2000,
                cols=len(ledger_headers(kind)),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Пустая таблица: не удалось создать лист «{title}»: {exc}. "
                f"{_permission_hint()}"
            ) from exc
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


def _worksheet_titles(book: Any) -> str:
    """Список имён вкладок для сообщения об ошибке."""
    return ", ".join(f"«{(ws.title or '').strip()}»" for ws in book.worksheets())


def _permission_hint() -> str:
    """Подсказка: таблицу нужно расшарить на сервисный аккаунт как редактор."""
    email = _service_account_email()
    target = email or "email из GOOGLE_SERVICE_ACCOUNT_JSON (client_email)"
    return (
        f"Откройте Google Таблицу → «Настройки доступа» → добавьте {target} "
        "с правом «Редактор». Viewer / комментирование недостаточно. "
        "Проверьте также GOOGLE_SHEETS_SPREADSHEET_ID."
    )


def _service_account_email() -> str:
    """Email сервисного аккаунта из ключа, если ключ читается."""
    try:
        info = _service_account_info(get_settings())
    except Exception:
        return ""
    return str(info.get("client_email") or "").strip()


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

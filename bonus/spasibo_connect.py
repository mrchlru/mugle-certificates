"""Загрузка связей «Спасибо» (users.id) ↔ карта Teams Bonus."""

from __future__ import annotations

import io
import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.employee_import import MAX_DATA_ROWS, MAX_UPLOAD_BYTES, normalize_header
from bonus.ledger import LedgerSheet
from bonus.models import Certificate, CertificateStatus, Role, User
from bonus.parts import list_parts
from bonus.services import BonusError, find_certificate

SHEET_NAME = "коннект"

_SHEET_ALIASES = frozenset(
    {
        "коннект",
        "connect",
        "spasibo",
        "spasibo connect",
        "спасибо",
        "коннект спасибо",
    }
)

_HEADER_KEYS: dict[str, set[str]] = {
    "spasibo_id": {
        "id спасибо",
        "id spasibo",
        "spasibo id",
        "spasibo_id",
        "users.id",
        "user id",
        "id",
    },
    "teams_card": {
        "номер карты teams",
        "номер карты teams bonus",
        "номер карты",
        "карта teams",
        "карта",
        "public_number",
        "public number",
        "mr",
        "код карты",
        "short code",
        "short_code",
        "token",
    },
    "last_name": {"фамилия", "last name", "last_name"},
    "first_name": {"имя", "first name", "first_name"},
}

# Сегменты, которые считаем «рабочими» картами сотрудника (не бартер / thanks).
_EMPLOYEE_SEGMENT_NAMES = frozenset(
    {
        "Карта сотрудника",
        "Ланч",
        "Недельные бонусы",
        "Ежемесячные бонусы",
    }
)


@dataclass(frozen=True)
class SpasiboConnectRow:
    """Строка файла до commit."""

    row_number: int
    spasibo_id: str
    teams_card: str
    preview_last_name: str
    preview_first_name: str
    teams_holder_name: str
    teams_public_number: str
    certificate_id: int | None
    error: str = ""
    is_duplicate: bool = False
    duplicate_reason: str = ""


@dataclass(frozen=True)
class SpasiboConnectPreview:
    """Предпросмотр без записи в БД."""

    batch_id: str
    filename: str
    total_rows: int
    ok_rows: int
    error_rows: int
    duplicate_rows: int
    rows: tuple[SpasiboConnectRow, ...]


@dataclass(frozen=True)
class SpasiboConnectResultRow:
    """Строка отчёта после commit."""

    row_number: int
    spasibo_id: str
    public_number: str
    link: str
    status: str
    reason: str


def preview_spasibo_workbook(
    db: Session,
    *,
    filename: str,
    payload: bytes,
) -> SpasiboConnectPreview:
    """Читает Excel, проверяет пары ID Спасибо + номер карты Teams."""
    _validate_upload(filename, payload)
    grid = _read_connect_grid(payload)
    if not grid:
        raise BonusError("В файле нет строк")
    headers = [str(cell or "") for cell in grid[0]]
    mapping = _map_headers(headers)
    rows: list[SpasiboConnectRow] = []
    for offset, cells in enumerate(grid[1:], start=2):
        if _row_empty(cells):
            continue
        if len(rows) >= MAX_DATA_ROWS:
            raise BonusError(f"В файле больше {MAX_DATA_ROWS} строк с данными")
        rows.append(_parse_row(db, cells, mapping, offset))
    if not rows:
        raise BonusError(f"На листе «Коннект» нет заполненных строк")
    _mark_file_duplicates(rows)
    _mark_existing_links(db, rows)
    batch_id = secrets.token_urlsafe(12)
    preview = SpasiboConnectPreview(
        batch_id=batch_id,
        filename=(filename or "upload.xlsx")[:200],
        total_rows=len(rows),
        ok_rows=sum(1 for row in rows if not row.error and not row.is_duplicate),
        error_rows=sum(1 for row in rows if row.error),
        duplicate_rows=sum(1 for row in rows if row.is_duplicate and not row.error),
        rows=tuple(rows),
    )
    _save_batch(preview)
    return preview


def load_spasibo_preview(batch_id: str) -> SpasiboConnectPreview:
    """Достаёт сохранённый предпросмотр."""
    payload = _batch_path(batch_id).read_text(encoding="utf-8")
    data = json.loads(payload)
    rows = tuple(SpasiboConnectRow(**item) for item in data["rows"])
    return SpasiboConnectPreview(
        batch_id=data["batch_id"],
        filename=data["filename"],
        total_rows=data["total_rows"],
        ok_rows=data["ok_rows"],
        error_rows=data["error_rows"],
        duplicate_rows=data["duplicate_rows"],
        rows=rows,
    )


def commit_spasibo_connect(
    db: Session,
    *,
    batch_id: str,
    base_url: str,
    apply_duplicates: bool = False,
    replace_conflicts: bool = False,
) -> list[SpasiboConnectResultRow]:
    """Записывает связи из предпросмотра."""
    preview = load_spasibo_preview(batch_id)
    results: list[SpasiboConnectResultRow] = []
    for row in preview.rows:
        if row.error:
            results.append(_result(row, "", "", "ошибка", row.error))
            continue
        if row.is_duplicate and not apply_duplicates:
            if not (
                replace_conflicts
                and row.duplicate_reason
                and "другой ID Спасибо" in row.duplicate_reason
            ):
                reason = row.duplicate_reason or "Уже привязано или дубль в файле"
                results.append(
                    _result(row, row.teams_public_number, "", "пропущена", reason)
                )
                continue
        assert row.certificate_id is not None
        certificate = db.get(Certificate, row.certificate_id)
        if certificate is None:
            results.append(_result(row, "", "", "ошибка", "Карта не найдена"))
            continue
        holder = certificate.holder
        if holder is None or holder.role != Role.employee:
            results.append(
                _result(row, certificate.public_number, "", "ошибка", "Не карта сотрудника")
            )
            continue
        eligible, why = _certificate_eligible(db, certificate)
        if not eligible:
            results.append(_result(row, certificate.public_number, "", "ошибка", why))
            continue
        conflict = _link_conflict(db, row.spasibo_id, holder.id, certificate.id)
        if conflict and not replace_conflicts:
            results.append(_result(row, certificate.public_number, "", "ошибка", conflict))
            continue
        holder.spasibo_user_id = row.spasibo_id
        holder.spasibo_certificate_id = certificate.id
        db.commit()
        link = f"{base_url.rstrip('/')}/c/{certificate.token}"
        status = "обновлено" if row.is_duplicate else "привязано"
        results.append(_result(row, certificate.public_number, link, status, ""))
    _clear_batch(batch_id)
    return results


def build_spasibo_connect_report(rows: list[SpasiboConnectResultRow]) -> bytes:
    """XLSX-отчёт для скачивания."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Коннект"
    sheet.append(
        ["Строка", "ID Спасибо", "Номер карты", "Статус", "Причина", "Ссылка"]
    )
    for row in rows:
        sheet.append(
            [
                row.row_number,
                _excel_safe(row.spasibo_id),
                _excel_safe(row.public_number),
                _excel_safe(row.status),
                _excel_safe(row.reason),
                _excel_safe(row.link),
            ]
        )
        if row.link:
            cell = sheet.cell(sheet.max_row, 6)
            cell.hyperlink = row.link
    widths = (10, 14, 16, 14, 40, 48)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_sample_workbook() -> bytes:
    """Пример Excel для HR."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Коннект"
    sheet.append(["ID Спасибо", "Номер карты Teams", "Фамилия", "Имя"])
    sheet.append([123, "MR-00142", "Иванов", "Иван"])
    sheet.append([456, "12345678", "Петрова", "Мария"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def save_spasibo_connect_report(batch_id: str, payload: bytes) -> str:
    """Сохраняет xlsx-отчёт и возвращает id для скачивания."""
    report_id = re.sub(r"[^A-Za-z0-9_-]", "", batch_id)[:40] or secrets.token_urlsafe(8)
    path = _batch_dir() / f"report_{report_id}.xlsx"
    path.write_bytes(payload)
    return report_id


def load_spasibo_connect_report(report_id: str) -> bytes:
    """Читает сохранённый отчёт."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", report_id)[:64]
    path = _batch_dir() / f"report_{safe}.xlsx"
    if not path.is_file():
        raise BonusError("Отчёт не найден")
    return path.read_bytes()


def _parse_row(
    db: Session,
    cells: tuple[object, ...],
    mapping: dict[str, int],
    row_number: int,
) -> SpasiboConnectRow:
    raw_id = _cell(cells, mapping, "spasibo_id")
    teams_card = _clean_text(_cell(cells, mapping, "teams_card"), 120)
    last_name = _clean_text(_cell(cells, mapping, "last_name"), 80)
    first_name = _clean_text(_cell(cells, mapping, "first_name"), 80)
    errors: list[str] = []
    spasibo_id = _parse_spasibo_id(raw_id)
    if spasibo_id is None:
        errors.append("ID Спасибо: положительное целое число")
        spasibo_id = ""
    if not teams_card:
        errors.append("Укажите номер карты Teams")
    holder_name = ""
    public_number = ""
    cert_id: int | None = None
    if teams_card and not errors:
        certificate = find_certificate(db, teams_card)
        if certificate is None:
            errors.append("Карта Teams не найдена")
        else:
            eligible, why = _certificate_eligible(db, certificate)
            if not eligible:
                errors.append(why)
            elif certificate.holder is None or certificate.holder.role != Role.employee:
                errors.append("Держатель карты не сотрудник")
            else:
                holder_name = certificate.holder.full_name
                public_number = certificate.public_number
                cert_id = certificate.id
    return SpasiboConnectRow(
        row_number=row_number,
        spasibo_id=spasibo_id,
        teams_card=teams_card,
        preview_last_name=last_name,
        preview_first_name=first_name,
        teams_holder_name=holder_name,
        teams_public_number=public_number,
        certificate_id=cert_id,
        error="; ".join(errors),
    )


def _parse_spasibo_id(raw: object) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return None
        value = int(raw)
    else:
        text = str(raw).strip()
        if not text.isdigit():
            return None
        value = int(text)
    if value <= 0:
        return None
    return str(value)


def _certificate_eligible(db: Session, certificate: Certificate) -> tuple[bool, str]:
    if certificate.is_deleted:
        return False, "Карта удалена"
    if certificate.status != CertificateStatus.active:
        return False, "Карта не активна"
    if certificate.ledger_sheet != LedgerSheet.employee.value:
        return False, "Не employee-лист (бартер/спасибо/ДР и т.п.)"
    parts = list_parts(db, certificate.id)
    if parts:
        names = {part.segment_name for part in parts if part.segment_name}
        if not names & _EMPLOYEE_SEGMENT_NAMES:
            return False, "Сегмент карты не для сотрудников"
    elif certificate.segment is not None:
        if certificate.segment.name not in _EMPLOYEE_SEGMENT_NAMES:
            return False, f"Сегмент «{certificate.segment.name}» не для коннекта"
    return True, ""


def _mark_file_duplicates(rows: list[SpasiboConnectRow]) -> None:
    seen_ids: dict[str, int] = {}
    seen_certs: dict[int, int] = {}
    for index, row in enumerate(rows):
        if row.error:
            continue
        if row.spasibo_id in seen_ids:
            rows[index] = _dup_row(row, "Повтор ID Спасибо в файле")
            continue
        seen_ids[row.spasibo_id] = row.row_number
        cert_id = row.certificate_id
        if cert_id is not None:
            if cert_id in seen_certs:
                rows[index] = _dup_row(row, "Одна карта Teams дважды в файле")
                continue
            seen_certs[cert_id] = row.row_number


def _mark_existing_links(db: Session, rows: list[SpasiboConnectRow]) -> None:
    for index, row in enumerate(rows):
        if row.error:
            continue
        assert row.certificate_id is not None
        certificate = db.get(Certificate, row.certificate_id)
        if certificate is None:
            continue
        user = certificate.holder
        if user is None:
            continue
        other = db.scalar(
            select(User).where(
                User.spasibo_user_id == row.spasibo_id,
                User.id != user.id,
            )
        )
        if other is not None:
            rows[index] = SpasiboConnectRow(
                **{
                    **asdict(row),
                    "error": f"ID Спасибо уже у другого держателя ({other.full_name})",
                }
            )
            continue
        if (
            user.spasibo_user_id == row.spasibo_id
            and user.spasibo_certificate_id == row.certificate_id
        ):
            rows[index] = _dup_row(row, "Уже привязано")
            continue
        if user.spasibo_user_id and user.spasibo_user_id != row.spasibo_id:
            rows[index] = _dup_row(
                row,
                f"У держателя другой ID Спасибо ({user.spasibo_user_id}); включите «Заменить конфликты»",
            )
            continue
        taken = db.scalar(
            select(User).where(
                User.spasibo_certificate_id == row.certificate_id,
                User.id != user.id,
            )
        )
        if taken is not None and taken.spasibo_user_id != row.spasibo_id:
            rows[index] = _dup_row(
                row,
                f"Карта привязана к ID Спасибо {taken.spasibo_user_id}",
            )


def _link_conflict(
    db: Session,
    spasibo_id: str,
    holder_id: int,
    certificate_id: int,
) -> str:
    other = db.scalar(
        select(User).where(
            User.spasibo_user_id == spasibo_id,
            User.id != holder_id,
        )
    )
    if other is not None:
        return f"ID Спасибо уже у {other.full_name}"
    taken = db.scalar(
        select(User).where(
            User.spasibo_certificate_id == certificate_id,
            User.id != holder_id,
        )
    )
    if taken is not None and taken.spasibo_user_id != spasibo_id:
        return f"Карта уже привязана к ID Спасибо {taken.spasibo_user_id}"
    return ""


def _dup_row(row: SpasiboConnectRow, reason: str) -> SpasiboConnectRow:
    return SpasiboConnectRow(
        **{**asdict(row), "is_duplicate": True, "duplicate_reason": reason}
    )


def _result(
    row: SpasiboConnectRow,
    public_number: str,
    link: str,
    status: str,
    reason: str,
) -> SpasiboConnectResultRow:
    return SpasiboConnectResultRow(
        row_number=row.row_number,
        spasibo_id=row.spasibo_id,
        public_number=public_number or row.teams_public_number,
        link=link,
        status=status,
        reason=reason,
    )


def _validate_upload(filename: str, payload: bytes) -> None:
    name = (filename or "").lower()
    if not name.endswith(".xlsx"):
        raise BonusError("Принимается только файл .xlsx")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise BonusError("Файл больше 2 МБ")
    if len(payload) < 32:
        raise BonusError("Файл пустой или повреждён")


def _read_connect_grid(payload: bytes) -> list[tuple[object, ...]]:
    try:
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except Exception as exc:
        raise BonusError("Не удалось открыть Excel-файл") from exc
    try:
        sheet, sheet_label = _pick_connect_sheet(workbook)
        rows = [tuple(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    if not rows:
        raise BonusError(f"Лист «{sheet_label}» пустой")
    headers = [str(cell or "") for cell in rows[0]]
    try:
        _map_headers(headers)
    except BonusError as exc:
        raise BonusError(
            f"{exc.message} (лист «{sheet_label}»). "
            "Переименуйте лист в «Коннект» или проверьте заголовки строки 1."
        ) from exc
    return rows


def _pick_connect_sheet(workbook: Workbook) -> tuple[Any, str]:
    """Выбирает лист «Коннект» или первый лист с подходящими заголовками."""
    names = [str(name) for name in (workbook.sheetnames or [])]
    if not names:
        raise BonusError("В книге нет листов")
    for name in names:
        folded = name.strip().casefold()
        if folded == SHEET_NAME or folded in _SHEET_ALIASES:
            return workbook[name], name
    for name in names:
        sheet = workbook[name]
        first = next(sheet.iter_rows(max_row=1, values_only=True), None)
        if first is None:
            continue
        headers = [str(cell or "") for cell in first]
        if _headers_complete(headers):
            return sheet, name
    raise BonusError(
        'Нет листа «Коннект» и не найден лист с колонками «ID Спасибо» и «Номер карты Teams». '
        f"Листы в файле: {', '.join(names[:8])}"
    )


def _headers_complete(headers: list[str]) -> bool:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(headers):
        key = _header_field(cell)
        if key is not None:
            mapping[key] = index
    return "spasibo_id" in mapping and "teams_card" in mapping


def _map_headers(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(headers):
        key = _header_field(cell)
        if key is not None:
            mapping[key] = index
    missing = [name for name in ("spasibo_id", "teams_card") if name not in mapping]
    if missing:
        raise BonusError(
            "Нужны колонки «ID Спасибо» и «Номер карты Teams» "
            "(фамилия/имя — только для проверки глазами)"
        )
    return mapping


def _header_field(cell: object) -> str | None:
    title = normalize_header(cell)
    if not title:
        return None
    for key, aliases in _HEADER_KEYS.items():
        if title in aliases:
            return key
    return None


def _cell(cells: tuple[object, ...], mapping: dict[str, int], key: str) -> object:
    index = mapping.get(key)
    if index is None or index >= len(cells):
        return ""
    return cells[index]


def _row_empty(cells: tuple[object, ...]) -> bool:
    return all(str(cell or "").strip() == "" for cell in cells)


def _clean_text(raw: object, limit: int) -> str:
    text = str(raw or "")
    text = "".join(ch for ch in text if ch >= " " or ch in "\t")
    return " ".join(text.split())[:limit]


def _excel_safe(value: str) -> str:
    text = str(value or "")
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _batch_dir() -> Path:
    path = get_settings().data_path() / "spasibo_connect"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _batch_path(batch_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", batch_id)[:64]
    if not safe:
        raise BonusError("Пакет загрузки не найден")
    path = _batch_dir() / f"{safe}.json"
    if not path.is_file():
        raise BonusError("Предпросмотр устарел — загрузите файл снова")
    return path


def _save_batch(preview: SpasiboConnectPreview) -> None:
    payload = {
        "batch_id": preview.batch_id,
        "filename": preview.filename,
        "total_rows": preview.total_rows,
        "ok_rows": preview.ok_rows,
        "error_rows": preview.error_rows,
        "duplicate_rows": preview.duplicate_rows,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "rows": [asdict(row) for row in preview.rows],
    }
    path = _batch_dir() / f"{preview.batch_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _clear_batch(batch_id: str) -> None:
    path = _batch_dir() / f"{re.sub(r'[^A-Za-z0-9_-]', '', batch_id)}.json"
    if path.is_file():
        path.unlink()

"""Массовая загрузка сертификатов на день рождения (шаблон «День рождения»)."""

from __future__ import annotations

import io
import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.ledger import LedgerSheet
from bonus.models import CardTemplate, Certificate, CertificateStatus, Segment, User
from bonus.services import BonusError, IssueDraft, issue_certificate
from bonus.values import MoneyError, parse_date, parse_money

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_DATA_ROWS = 1000

BIRTHDAY_SEGMENT_NAME = "День рождения"
BIRTHDAY_TEMPLATE_NAME = "День рождения"

_HEADER_ALIASES: dict[str, set[str]] = {
    "full_name": {"фио", "ф.и.о.", "имя", "сотрудник"},
    "company": {"ооо", "юрлицо", "организация", "company"},
    "department": {"ресторан / офис", "ресторан/офис", "ресторан", "офис", "отдел"},
    "job_title": {"должность", "позиция"},
    "birth_date": {"дата рождения", "др", "birth_date", "birthday", "день рождения"},
    "expires_on": {
        "срок действия",
        "срок",
        "действует до",
        "годен до",
        "expires",
    },
    "nominal": {"номинал", "сумма", "лимит"},
    "phone": {"телефон", "тел", "phone"},
    "note": {"комментарий", "примечание", "note"},
}

_SAMPLE_HEADERS = (
    "ФИО",
    "ООО",
    "Ресторан / офис",
    "Должность",
    "Дата рождения",
    "Срок действия",
    "Номинал",
    "Комментарий",
    "Телефон",
)
_SAMPLE_WIDTHS = (24, 16, 18, 18, 14, 14, 12, 24, 14)
_SAMPLE_ROW = (
    "Иванов Иван Иванович",
    "МУГЛЕ",
    "ХВВ",
    "Официант",
    datetime(1990, 5, 15),
    datetime(2026, 12, 31),
    3000,
    "День рождения",
    "+7 900 000-00-00",
)


@dataclass(frozen=True)
class BirthdayImportRow:
    """Строка Excel до выпуска."""

    row_number: int
    full_name: str
    company: str
    department: str
    job_title: str
    birth_date: date | None
    expires_on: date | None
    nominal: int
    phone: str
    note: str
    error: str = ""
    is_duplicate: bool = False


@dataclass(frozen=True)
class BirthdayImportPreview:
    """Предпросмотр без записи в БД."""

    batch_id: str
    filename: str
    total_rows: int
    ok_rows: int
    error_rows: int
    duplicate_rows: int
    rows: tuple[BirthdayImportRow, ...]


@dataclass(frozen=True)
class BirthdayImportResultRow:
    """Итог по одной строке."""

    row_number: int
    full_name: str
    public_number: str
    link: str
    status: str
    reason: str


def build_sample_workbook() -> bytes:
    """Шаблон Excel для загрузки карт на ДР."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Карты"
    sheet.append(list(_SAMPLE_HEADERS))
    sheet.append(list(_SAMPLE_ROW))
    for index, width in enumerate(_SAMPLE_WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def preview_birthday_workbook(
    db: Session,
    *,
    filename: str,
    payload: bytes,
) -> BirthdayImportPreview:
    """Читает Excel и валидирует строки."""
    _validate_upload(filename, payload)
    _resolve_catalog(db)
    grid = _read_cards_grid(payload)
    if not grid:
        raise BonusError("В файле нет строк")
    headers = [str(cell or "") for cell in grid[0]]
    mapping = _map_headers(headers)
    rows: list[BirthdayImportRow] = []
    for offset, cells in enumerate(grid[1:], start=2):
        if _row_empty(cells) or not str(_cell(cells, mapping, "full_name") or "").strip():
            continue
        if len(rows) >= MAX_DATA_ROWS:
            raise BonusError(f"В файле больше {MAX_DATA_ROWS} строк с данными")
        rows.append(_parse_row(cells, mapping, offset))
    if not rows:
        raise BonusError("На листе «Карты» нет заполненных строк")
    _mark_duplicates(db, rows)
    batch_id = secrets.token_urlsafe(12)
    preview = BirthdayImportPreview(
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


def load_preview(batch_id: str) -> BirthdayImportPreview:
    """Возвращает сохранённый предпросмотр."""
    data = json.loads(_batch_path(batch_id).read_text(encoding="utf-8"))
    rows = tuple(_row_from_dict(item) for item in data["rows"])
    return BirthdayImportPreview(
        batch_id=data["batch_id"],
        filename=data["filename"],
        total_rows=data["total_rows"],
        ok_rows=data["ok_rows"],
        error_rows=data["error_rows"],
        duplicate_rows=data["duplicate_rows"],
        rows=rows,
    )


def commit_birthday_import(
    db: Session,
    *,
    batch_id: str,
    actor: User,
    base_url: str,
    create_duplicates: bool = False,
) -> tuple[list[BirthdayImportResultRow], list[int]]:
    """Выпускает карты из предпросмотра."""
    preview = load_preview(batch_id)
    catalog = _resolve_catalog(db)
    segment: Segment = catalog["segment"]
    template: CardTemplate = catalog["template"]
    results: list[BirthdayImportResultRow] = []
    issued_ids: list[int] = []
    for row in preview.rows:
        if row.error:
            results.append(_result(row, "", "", "ошибка", row.error))
            continue
        if row.is_duplicate and not create_duplicates:
            results.append(
                _result(
                    row,
                    "",
                    "",
                    "пропущена",
                    "Возможный дубль (ФИО + ООО + дата рождения)",
                )
            )
            continue
        try:
            discount = template.discount_percent or segment.discount_percent or 0
            draft = _draft_from_row(
                row,
                segment.id,
                template.id,
                color=template.color or "#00A8A8",
                discount_percent=discount,
            )
            issued = issue_certificate(db, draft, actor, sync_external=False)
            link = f"{base_url.rstrip('/')}/c/{issued.certificate.token}"
            issued_ids.append(issued.certificate.id)
            results.append(
                _result(row, issued.certificate.public_number, link, "создана", "")
            )
        except BonusError as exc:
            db.rollback()
            results.append(_result(row, "", "", "ошибка", exc.message))
        except Exception:
            db.rollback()
            results.append(_result(row, "", "", "ошибка", "Не удалось выписать карту"))
    _clear_batch(batch_id)
    return results, issued_ids


def build_birthday_import_report(rows: list[BirthdayImportResultRow]) -> bytes:
    """Excel-таблица с созданными сертификатами."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Дни рождения"
    sheet.append(("Строка", "ФИО", "Номер карты", "Ссылка", "Статус", "Причина"))
    for row in rows:
        sheet.append(
            [
                row.row_number,
                _excel_safe(row.full_name),
                _excel_safe(row.public_number),
                _excel_safe(row.link),
                _excel_safe(row.status),
                _excel_safe(row.reason),
            ]
        )
        if row.link:
            cell = sheet.cell(sheet.max_row, 4)
            cell.hyperlink = row.link
    for index, width in enumerate((10, 28, 14, 48, 12, 40), start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def save_birthday_import_report(batch_id: str, payload: bytes) -> str:
    """Сохраняет отчёт и возвращает id для скачивания."""
    report_id = re.sub(r"[^A-Za-z0-9_-]", "", batch_id)[:40] or secrets.token_urlsafe(8)
    path = _batch_dir() / f"birthday_report_{report_id}.xlsx"
    path.write_bytes(payload)
    return report_id


def load_birthday_import_report(report_id: str) -> bytes:
    """Читает xlsx-отчёт."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", report_id)[:64]
    path = _batch_dir() / f"birthday_report_{safe}.xlsx"
    if not path.is_file():
        raise BonusError("Отчёт не найден")
    return path.read_bytes()


def _resolve_catalog(db: Session) -> dict[str, Any]:
    segment = _require_named(db, Segment, BIRTHDAY_SEGMENT_NAME, "сегмент")
    template = _require_named(db, CardTemplate, BIRTHDAY_TEMPLATE_NAME, "шаблон")
    return {"segment": segment, "template": template}


def _require_named(db: Session, model: type[Segment] | type[CardTemplate], name: str, kind: str):
    found = db.scalar(
        select(model).where(
            model.name == name,
            model.is_deleted.is_(False),
            model.is_active.is_(True),
        )
    )
    if found is None:
        found = db.scalar(
            select(model).where(
                model.name.ilike(name),
                model.is_deleted.is_(False),
                model.is_active.is_(True),
            )
        )
    if found is None:
        raise BonusError(f"Создайте в Оформлении {kind} «{name}»")
    return found


def _parse_row(
    cells: tuple[object, ...],
    mapping: dict[str, int],
    row_number: int,
) -> BirthdayImportRow:
    errors: list[str] = []
    full_name = _clean_text(_cell(cells, mapping, "full_name"), 200)
    company = _clean_text(_cell(cells, mapping, "company"), 200)
    department = _clean_text(_cell(cells, mapping, "department"), 120)
    job_title = _clean_text(_cell(cells, mapping, "job_title"), 120)
    phone = _clean_phone(_cell(cells, mapping, "phone"))
    note = _clean_text(_cell(cells, mapping, "note"), 500)
    if len(" ".join(full_name.split())) < 2:
        errors.append("ФИО: укажите имя и фамилию")
    if not company:
        errors.append("ООО: обязательное поле")
    if not department:
        errors.append("Ресторан / офис: обязательное поле")
    if not job_title:
        errors.append("Должность: обязательное поле")
    try:
        birth_date = _as_date(_cell(cells, mapping, "birth_date"), "Дата рождения")
    except BonusError as exc:
        errors.append(exc.message)
        birth_date = None
    try:
        expires_on = _as_date(_cell(cells, mapping, "expires_on"), "Срок действия")
    except BonusError as exc:
        errors.append(exc.message)
        expires_on = None
    try:
        nominal = _parse_nominal(_cell(cells, mapping, "nominal"))
    except BonusError as exc:
        errors.append(exc.message)
        nominal = 0
    return BirthdayImportRow(
        row_number=row_number,
        full_name=full_name,
        company=company,
        department=department,
        job_title=job_title,
        birth_date=birth_date,
        expires_on=expires_on,
        nominal=nominal,
        phone=phone,
        note=note,
        error="; ".join(errors),
    )


def _draft_from_row(
    row: BirthdayImportRow,
    segment_id: int,
    template_id: int,
    *,
    color: str = "#00A8A8",
    discount_percent: int = 0,
) -> IssueDraft:
    if row.birth_date is None or row.expires_on is None:
        raise BonusError("Дата рождения и срок действия обязательны")
    return IssueDraft(
        full_name=row.full_name,
        login="",
        password="",
        nominal=row.nominal,
        balance=None,
        note=row.note,
        expires_on=row.expires_on,
        segment_id=segment_id,
        template_id=template_id,
        color=color,
        department=row.department,
        job_title=row.job_title,
        phone=row.phone,
        discount_percent=discount_percent,
        segment_name=BIRTHDAY_SEGMENT_NAME,
        single_use=True,
        ledger_sheet=LedgerSheet.birthday.value,
        company=row.company,
        birth_date=row.birth_date,
        show_wallet_buttons=False,
        template_name=BIRTHDAY_TEMPLATE_NAME,
    )


def _mark_duplicates(db: Session, rows: list[BirthdayImportRow]) -> None:
    existing = _existing_keys(db)
    for index, row in enumerate(rows):
        if row.error or row.birth_date is None:
            continue
        key = (
            row.full_name.casefold(),
            row.company.casefold(),
            row.birth_date.isoformat(),
        )
        if key in existing:
            rows[index] = BirthdayImportRow(**{**asdict(row), "is_duplicate": True})


def _existing_keys(db: Session) -> set[tuple[str, str, str]]:
    rows = db.execute(
        select(User.full_name, Certificate.company, Certificate.birth_date)
        .join(User, Certificate.holder_id == User.id)
        .where(
            Certificate.is_deleted.is_(False),
            Certificate.status == CertificateStatus.active,
            Certificate.ledger_sheet == LedgerSheet.birthday.value,
        )
    ).all()
    return {
        (
            str(full_name or "").casefold(),
            str(company or "").casefold(),
            birth_date.isoformat() if birth_date is not None else "",
        )
        for full_name, company, birth_date in rows
        if birth_date is not None
    }


def _result(
    row: BirthdayImportRow,
    public_number: str,
    link: str,
    status: str,
    reason: str,
) -> BirthdayImportResultRow:
    return BirthdayImportResultRow(
        row_number=row.row_number,
        full_name=row.full_name,
        public_number=public_number,
        link=link,
        status=status,
        reason=reason,
    )


def _row_from_dict(data: dict[str, Any]) -> BirthdayImportRow:
    birth = data.get("birth_date")
    expires = data.get("expires_on")
    return BirthdayImportRow(
        row_number=int(data["row_number"]),
        full_name=str(data["full_name"]),
        company=str(data["company"]),
        department=str(data["department"]),
        job_title=str(data["job_title"]),
        birth_date=date.fromisoformat(birth) if birth else None,
        expires_on=date.fromisoformat(expires) if expires else None,
        nominal=int(data["nominal"]),
        phone=str(data.get("phone") or ""),
        note=str(data.get("note") or ""),
        error=str(data.get("error") or ""),
        is_duplicate=bool(data.get("is_duplicate")),
    )


def _save_batch(preview: BirthdayImportPreview) -> None:
    rows = []
    for row in preview.rows:
        item = asdict(row)
        item["birth_date"] = row.birth_date.isoformat() if row.birth_date else None
        item["expires_on"] = row.expires_on.isoformat() if row.expires_on else None
        rows.append(item)
    payload = {
        "batch_id": preview.batch_id,
        "filename": preview.filename,
        "total_rows": preview.total_rows,
        "ok_rows": preview.ok_rows,
        "error_rows": preview.error_rows,
        "duplicate_rows": preview.duplicate_rows,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }
    path = _batch_dir() / f"{preview.batch_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _validate_upload(filename: str, payload: bytes) -> None:
    if not (filename or "").lower().endswith(".xlsx"):
        raise BonusError("Принимается только файл .xlsx")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise BonusError("Файл больше 2 МБ")
    if len(payload) < 32:
        raise BonusError("Файл пустой или повреждён")


def _read_cards_grid(payload: bytes) -> list[tuple[object, ...]]:
    try:
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except Exception as exc:
        raise BonusError("Не удалось открыть Excel-файл") from exc
    sheet = None
    for name in workbook.sheetnames or []:
        if str(name).strip().casefold() == "карты":
            sheet = workbook[name]
            break
    if sheet is None:
        raise BonusError("В книге нет листа «Карты»")
    return [tuple(row) for row in sheet.iter_rows(values_only=True)]


def _map_headers(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(headers):
        key = _header_key(cell)
        if key is not None:
            mapping[key] = index
    required = [
        "full_name",
        "company",
        "department",
        "job_title",
        "birth_date",
        "expires_on",
        "nominal",
    ]
    missing = []
    labels = {
        "full_name": "ФИО",
        "company": "ООО",
        "department": "Ресторан / офис",
        "job_title": "Должность",
        "birth_date": "Дата рождения",
        "expires_on": "Срок действия",
        "nominal": "Номинал",
    }
    for key in required:
        if key not in mapping:
            missing.append(labels[key])
    if missing:
        raise BonusError("В файле не хватает колонок: " + ", ".join(missing))
    return mapping


def _header_key(cell: object) -> str | None:
    title = " ".join(str(cell or "").strip().casefold().split()).replace("ё", "е")
    if not title:
        return None
    for key, aliases in _HEADER_ALIASES.items():
        if title in aliases:
            return key
    return None


def _parse_nominal(raw: object) -> int:
    try:
        value = parse_money(raw, blank_as_none=True)
    except MoneyError as exc:
        raise BonusError(f"Номинал: {exc.message}") from exc
    if value is None or value <= 0:
        raise BonusError("Номинал: укажите число больше нуля")
    return value


def _as_date(raw: object, label: str) -> date:
    if raw is None or str(raw).strip() == "":
        raise BonusError(f"{label}: обязательное поле")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    parsed = parse_date(raw)
    if isinstance(parsed, date):
        return parsed
    raise BonusError(f"{label}: не удалось разобрать дату")


def _clean_text(raw: object, limit: int) -> str:
    text = str(raw or "")
    text = "".join(ch for ch in text if ch >= " " or ch in "\t")
    return " ".join(text.split())[:limit]


def _clean_phone(raw: object) -> str:
    text = str(raw or "").strip()
    return text[:40]


def _cell(cells: tuple[object, ...], mapping: dict[str, int], key: str) -> object:
    index = mapping.get(key)
    if index is None or index >= len(cells):
        return ""
    return cells[index]


def _row_empty(cells: tuple[object, ...]) -> bool:
    return all(str(cell or "").strip() == "" for cell in cells)


def _excel_safe(value: str) -> str:
    text = str(value or "")
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _batch_dir() -> Path:
    path = get_settings().data_path() / "birthday_import"
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


def _clear_batch(batch_id: str) -> None:
    path = _batch_dir() / f"{re.sub(r'[^A-Za-z0-9_-]', '', batch_id)}.json"
    if path.is_file():
        path.unlink()

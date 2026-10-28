"""Массовая загрузка карт сотрудников из Excel (месяц / месяц+день / месяц+неделя)."""

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
from bonus.ledger import LedgerSheet
from bonus.models import AccrualPeriod, CardTemplate, Certificate, CertificateStatus, Segment, User
from bonus.parts import PartDraft
from bonus.catalog import normalize_color
from bonus.services import BonusError, IssueDraft, issue_certificate, normalize_discount
from bonus.values import MoneyError, parse_money
from bonus.weekdays import (
    ALL_DAYS,
    WEEKDAYS,
    normalize_single_weekday,
    parse_weekday_labels,
)

# Лимиты загрузки (можно переопределить в Settings позже).
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_DATA_ROWS = 1000

# Каталог: имена сегментов и шаблонов в одном месте.
KIND_EMPLOYEE = "employee_month"
KIND_COMBO_DAY = "combo_month_day"
KIND_COMBO_WEEK = "combo_month_week"

CATALOG: dict[str, dict[str, str]] = {
    KIND_EMPLOYEE: {
        "title": "Карта сотрудника",
        "segment_employee": "Карта сотрудника",
        "template": "Карта сотрудника",
        "marker": "номинал, р в месяц",
    },
    KIND_COMBO_DAY: {
        "title": "Комбо: месяц + день",
        "segment_employee": "Карта сотрудника",
        "segment_extra": "Ланч",
        "template": "Комбо карта",
        "marker": "ланч, р в день",
    },
    KIND_COMBO_WEEK: {
        "title": "Комбо: месяц + неделя",
        "segment_employee": "Карта сотрудника",
        "segment_extra": "Недельные бонусы",
        "template": "Комбо карта: месяц + неделя",
        "marker": "недельные бонусы, р в неделю",
    },
}

_HEADER_KEYS: dict[str, set[str]] = {
    "full_name": {"фио", "ф.и.о.", "имя", "сотрудник"},
    "company": {"ооо", "юрлицо", "организация", "company"},
    "department": {"ресторан / офис", "ресторан/офис", "ресторан", "офис", "отдел"},
    "job_title": {"должность", "позиция"},
    "phone": {"телефон", "тел", "phone"},
    "nominal_month": {"номинал, р в месяц", "номинал р в месяц"},
    "nominal_employee": {
        "карта сотрудника, р в месяц",
        "карта сотрудника р в месяц",
    },
    "nominal_lunch": {"ланч, р в день", "ланч р в день"},
    "nominal_weekly": {
        "недельные бонусы, р в неделю",
        "недельные бонусы р в неделю",
    },
    "use_days": {"дни использования"},
    "accrual_lunch": {"дни начисления · ланч", "дни начисления ланч", "дни начисления. ланч"},
    "template": {"шаблон баннера", "шаблон", "баннер"},
    "discount": {"процент на карточке", "процент", "скидка"},
    "color": {"цвет карточки", "цвет"},
    "note": {"комментарий", "примечание"},
}


@dataclass(frozen=True)
class EmployeeImportRow:
    """Разобранная строка файла до выпуска."""

    row_number: int
    full_name: str
    company: str
    department: str
    job_title: str
    phone: str
    note: str
    discount_percent: int
    color: str
    template_name: str
    use_days: str
    accrual_days_extra: str
    nominal_employee: int
    nominal_extra: int
    error: str = ""
    is_duplicate: bool = False


@dataclass(frozen=True)
class EmployeeImportPreview:
    """Предпросмотр без записи в БД."""

    batch_id: str
    filename: str
    kind: str
    kind_title: str
    total_rows: int
    ok_rows: int
    error_rows: int
    duplicate_rows: int
    rows: tuple[EmployeeImportRow, ...]


@dataclass(frozen=True)
class EmployeeImportResultRow:
    """Строка итогового отчёта."""

    row_number: int
    full_name: str
    public_number: str
    link: str
    status: str
    reason: str


def normalize_header(value: object) -> str:
    """Нормализует заголовок: регистр, пробелы, ё→е, ₽→р."""
    text = str(value or "").strip().casefold().replace("ё", "е").replace("₽", "р")
    text = re.sub(r"\s+", " ", text)
    return text


def detect_employee_file_kind(headers: list[str]) -> str:
    """Определяет тип файла по маркерам заголовков. Ровно один тип."""
    normalized = {normalize_header(item) for item in headers if str(item or "").strip()}
    matched: list[str] = []
    for kind, meta in CATALOG.items():
        if meta["marker"] in normalized:
            matched.append(kind)
    if len(matched) == 1:
        return matched[0]
    if not matched:
        raise BonusError(
            "Не удалось определить тип файла. Нужен шаблон "
            "«Карты сотрудников» (месяц / месяц+день / месяц+неделя)."
        )
    titles = ", ".join(CATALOG[item]["title"] for item in matched)
    raise BonusError(f"В файле смешаны признаки нескольких типов: {titles}")


def preview_employee_workbook(
    db: Session,
    *,
    filename: str,
    payload: bytes,
) -> EmployeeImportPreview:
    """Читает Excel, проверяет каталог и строки, не пишет в БД."""
    _validate_upload(filename, payload)
    grid = _read_cards_grid(payload)
    if not grid:
        raise BonusError("В файле нет строк")
    headers = [str(cell or "") for cell in grid[0]]
    kind = detect_employee_file_kind(headers)
    catalog = _resolve_catalog(db, kind)
    mapping = _map_headers(headers, kind)
    rows: list[EmployeeImportRow] = []
    for offset, cells in enumerate(grid[1:], start=2):
        if _row_empty(cells) or not str(_cell(cells, mapping, "full_name") or "").strip():
            continue
        if len(rows) >= MAX_DATA_ROWS:
            raise BonusError(f"В файле больше {MAX_DATA_ROWS} строк с данными")
        rows.append(_parse_row(cells, mapping, kind, catalog, offset))
    if not rows:
        raise BonusError("На листе «Карты» нет заполненных строк")
    _mark_duplicates(db, kind, rows)
    batch_id = secrets.token_urlsafe(12)
    preview = EmployeeImportPreview(
        batch_id=batch_id,
        filename=(filename or "upload.xlsx")[:200],
        kind=kind,
        kind_title=CATALOG[kind]["title"],
        total_rows=len(rows),
        ok_rows=sum(1 for row in rows if not row.error and not row.is_duplicate),
        error_rows=sum(1 for row in rows if row.error),
        duplicate_rows=sum(1 for row in rows if row.is_duplicate and not row.error),
        rows=tuple(rows),
    )
    _save_batch(preview, catalog)
    return preview


def load_preview(batch_id: str) -> EmployeeImportPreview:
    """Достаёт сохранённый предпросмотр."""
    payload = _batch_path(batch_id).read_text(encoding="utf-8")
    data = json.loads(payload)
    rows = tuple(EmployeeImportRow(**item) for item in data["rows"])
    return EmployeeImportPreview(
        batch_id=data["batch_id"],
        filename=data["filename"],
        kind=data["kind"],
        kind_title=data["kind_title"],
        total_rows=data["total_rows"],
        ok_rows=data["ok_rows"],
        error_rows=data["error_rows"],
        duplicate_rows=data["duplicate_rows"],
        rows=rows,
    )


def commit_employee_import(
    db: Session,
    *,
    batch_id: str,
    actor: User,
    base_url: str,
    create_duplicates: bool = False,
) -> tuple[list[EmployeeImportResultRow], list[int]]:
    """Выпускает карты из предпросмотра. Возвращает отчёт и id созданных."""
    preview = load_preview(batch_id)
    catalog = _resolve_catalog(db, preview.kind)
    results: list[EmployeeImportResultRow] = []
    issued_ids: list[int] = []
    for row in preview.rows:
        if row.error:
            results.append(
                EmployeeImportResultRow(
                    row_number=row.row_number,
                    full_name=row.full_name,
                    public_number="",
                    link="",
                    status="ошибка",
                    reason=row.error,
                )
            )
            continue
        if row.is_duplicate and not create_duplicates:
            results.append(
                EmployeeImportResultRow(
                    row_number=row.row_number,
                    full_name=row.full_name,
                    public_number="",
                    link="",
                    status="пропущена",
                    reason="Возможный дубль (ФИО + ООО + ресторан / офис)",
                )
            )
            continue
        try:
            draft = _draft_from_row(row, preview.kind, catalog)
            issued = issue_certificate(db, draft, actor, sync_external=False)
            link = f"{base_url.rstrip('/')}/c/{issued.certificate.token}"
            issued_ids.append(issued.certificate.id)
            results.append(
                EmployeeImportResultRow(
                    row_number=row.row_number,
                    full_name=row.full_name,
                    public_number=issued.certificate.public_number,
                    link=link,
                    status="создана",
                    reason="",
                )
            )
        except BonusError as exc:
            db.rollback()
            results.append(
                EmployeeImportResultRow(
                    row_number=row.row_number,
                    full_name=row.full_name,
                    public_number="",
                    link="",
                    status="ошибка",
                    reason=exc.message,
                )
            )
        except Exception:
            db.rollback()
            results.append(
                EmployeeImportResultRow(
                    row_number=row.row_number,
                    full_name=row.full_name,
                    public_number="",
                    link="",
                    status="ошибка",
                    reason="Не удалось выписать карту",
                )
            )
    _clear_batch(batch_id)
    return results, issued_ids


def build_employee_import_report(rows: list[EmployeeImportResultRow]) -> bytes:
    """XLSX-отчёт для скачивания и рассылки."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Отчёт"
    headers = ("Строка", "ФИО", "Номер карты", "Ссылка", "Статус", "Причина")
    sheet.append(list(headers))
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
    widths = (10, 28, 14, 48, 12, 40)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _validate_upload(filename: str, payload: bytes) -> None:
    name = (filename or "").lower()
    if not name.endswith(".xlsx"):
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
    names = [str(name) for name in (workbook.sheetnames or [])]
    sheet = None
    for name in names:
        if name.strip().casefold() == "карты":
            sheet = workbook[name]
            break
    if sheet is None:
        raise BonusError("В книге нет листа «Карты»")
    return [tuple(row) for row in sheet.iter_rows(values_only=True)]


def _map_headers(headers: list[str], kind: str) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(headers):
        key = _header_field(cell)
        if key is not None:
            mapping[key] = index
    required = ["full_name", "company", "department", "job_title"]
    if kind == KIND_EMPLOYEE:
        required.append("nominal_month")
    else:
        required.append("nominal_employee")
        if kind == KIND_COMBO_DAY:
            required.append("nominal_lunch")
        else:
            required.append("nominal_weekly")
    missing = [name for name in required if name not in mapping]
    if missing:
        raise BonusError("В файле не хватает обязательных колонок")
    return mapping


def _header_field(cell: object) -> str | None:
    title = normalize_header(cell)
    if not title:
        return None
    for key, aliases in _HEADER_KEYS.items():
        if title in aliases:
            return key
    return None


def _resolve_catalog(db: Session, kind: str) -> dict[str, Any]:
    meta = CATALOG[kind]
    employee = _require_segment(db, meta["segment_employee"])
    if employee.accrual_period != AccrualPeriod.monthly:
        raise BonusError(
            f"Сегмент «{employee.name}» должен быть с ежемесячным начислением"
        )
    extra = None
    if "segment_extra" in meta:
        extra = _require_segment(db, meta["segment_extra"])
        if kind == KIND_COMBO_DAY and extra.accrual_period != AccrualPeriod.daily:
            raise BonusError(
                f"Сегмент «{extra.name}» должен быть с ежедневным начислением"
            )
        if kind == KIND_COMBO_WEEK and extra.accrual_period != AccrualPeriod.weekly:
            raise BonusError(
                f"Сегмент «{extra.name}» должен быть с еженедельным начислением"
            )
    template = _require_template(db, meta["template"])
    return {
        "kind": kind,
        "employee": employee,
        "extra": extra,
        "template": template,
        "meta": meta,
    }


def _require_segment(db: Session, name: str) -> Segment:
    found = db.scalar(
        select(Segment).where(
            Segment.name == name,
            Segment.is_deleted.is_(False),
            Segment.is_active.is_(True),
        )
    )
    if found is None:
        found = db.scalar(
            select(Segment).where(
                Segment.name.ilike(name),
                Segment.is_deleted.is_(False),
                Segment.is_active.is_(True),
            )
        )
    if found is None:
        raise BonusError(f"Создайте в Оформлении сегмент «{name}»")
    return found


def _require_template(db: Session, name: str) -> CardTemplate:
    found = db.scalar(
        select(CardTemplate).where(
            CardTemplate.name == name,
            CardTemplate.is_deleted.is_(False),
            CardTemplate.is_active.is_(True),
        )
    )
    if found is None:
        found = db.scalar(
            select(CardTemplate).where(
                CardTemplate.name.ilike(name),
                CardTemplate.is_deleted.is_(False),
                CardTemplate.is_active.is_(True),
            )
        )
    if found is None:
        raise BonusError(f"Создайте в Оформлении шаблон «{name}»")
    return found


def _parse_row(
    cells: tuple[object, ...],
    mapping: dict[str, int],
    kind: str,
    catalog: dict[str, Any],
    row_number: int,
) -> EmployeeImportRow:
    employee: Segment = catalog["employee"]
    extra: Segment | None = catalog["extra"]
    template: CardTemplate = catalog["template"]
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
        if kind == KIND_EMPLOYEE:
            nominal_employee = _parse_nominal(
                _cell(cells, mapping, "nominal_month"), "Номинал, ₽ в месяц"
            )
            nominal_extra = 0
        else:
            nominal_employee = _parse_nominal(
                _cell(cells, mapping, "nominal_employee"),
                "Карта сотрудника, ₽ в месяц",
            )
            if kind == KIND_COMBO_DAY:
                nominal_extra = _parse_nominal(
                    _cell(cells, mapping, "nominal_lunch"), "Ланч, ₽ в день"
                )
            else:
                nominal_extra = _parse_nominal(
                    _cell(cells, mapping, "nominal_weekly"),
                    "Недельные бонусы, ₽ в неделю",
                )
    except BonusError as exc:
        errors.append(exc.message)
        nominal_employee = 0
        nominal_extra = 0

    use_days = employee.use_days or ALL_DAYS
    raw_use = _cell(cells, mapping, "use_days")
    if str(raw_use or "").strip():
        try:
            use_days = parse_weekday_labels(raw_use, default=use_days)
        except ValueError as exc:
            errors.append(f"Дни использования: неизвестный день «{exc.args[0]}»")

    accrual_extra = ""
    if extra is not None:
        accrual_extra = extra.accrual_days or (
            "5" if extra.accrual_period == AccrualPeriod.weekly else WEEKDAYS
        )
        if kind == KIND_COMBO_DAY:
            raw_accrual = _cell(cells, mapping, "accrual_lunch")
            if str(raw_accrual or "").strip():
                try:
                    accrual_extra = parse_weekday_labels(
                        raw_accrual, default=accrual_extra
                    )
                except ValueError as exc:
                    errors.append(
                        f"Дни начисления · Ланч: неизвестный день «{exc.args[0]}»"
                    )

    template_name = _clean_text(_cell(cells, mapping, "template"), 120)
    if template_name and normalize_header(template_name) != normalize_header(
        template.name
    ):
        errors.append(
            f"Шаблон баннера: для этого файла нужен «{template.name}», указан «{template_name}»"
        )

    discount = template.discount_percent or employee.discount_percent or 0
    raw_discount = _cell(cells, mapping, "discount")
    if str(raw_discount or "").strip() != "":
        try:
            discount = _parse_discount(raw_discount)
        except BonusError as exc:
            errors.append(exc.message)

    color = template.color or "#00A8A8"
    raw_color = _cell(cells, mapping, "color")
    if str(raw_color or "").strip() != "":
        try:
            color = normalize_color(str(raw_color).strip())
        except BonusError as exc:
            errors.append(f"Цвет карточки: {exc.message}")

    return EmployeeImportRow(
        row_number=row_number,
        full_name=full_name,
        company=company,
        department=department,
        job_title=job_title,
        phone=phone,
        note=note,
        discount_percent=discount,
        color=color,
        template_name=template_name or template.name,
        use_days=use_days,
        accrual_days_extra=accrual_extra,
        nominal_employee=nominal_employee,
        nominal_extra=nominal_extra,
        error="; ".join(errors),
    )


def _mark_duplicates(
    db: Session, kind: str, rows: list[EmployeeImportRow]
) -> None:
    """Помечает строки, похожие на уже выпущенные активные карты."""
    keys = {
        (
            row.full_name.casefold(),
            row.company.casefold(),
            row.department.casefold(),
        )
        for row in rows
        if not row.error
    }
    if not keys:
        return
    existing = _existing_employee_keys(db)
    for index, row in enumerate(rows):
        if row.error:
            continue
        key = (
            row.full_name.casefold(),
            row.company.casefold(),
            row.department.casefold(),
        )
        if key in existing:
            rows[index] = EmployeeImportRow(**{**asdict(row), "is_duplicate": True})


def _existing_employee_keys(db: Session) -> set[tuple[str, str, str]]:
    rows = db.execute(
        select(User.full_name, Certificate.company, User.department)
        .join(User, Certificate.holder_id == User.id)
        .where(
            Certificate.is_deleted.is_(False),
            Certificate.status == CertificateStatus.active,
            Certificate.ledger_sheet == LedgerSheet.employee.value,
        )
    ).all()
    return {
        (
            str(full_name or "").casefold(),
            str(company or "").casefold(),
            str(department or "").casefold(),
        )
        for full_name, company, department in rows
    }


def _draft_from_row(
    row: EmployeeImportRow, kind: str, catalog: dict[str, Any]
) -> IssueDraft:
    employee: Segment = catalog["employee"]
    extra: Segment | None = catalog["extra"]
    template: CardTemplate = catalog["template"]
    parts: list[PartDraft] = [
        PartDraft(
            segment_id=employee.id,
            nominal=row.nominal_employee,
            use_days=row.use_days,
            accrual_days=employee.accrual_days or WEEKDAYS,
            sort_order=1,
        )
    ]
    if extra is not None and row.nominal_extra > 0:
        extra_days = row.accrual_days_extra or extra.accrual_days or WEEKDAYS
        if extra.accrual_period == AccrualPeriod.weekly:
            extra_days = normalize_single_weekday(extra_days, default="5")
        parts.append(
            PartDraft(
                segment_id=extra.id,
                nominal=row.nominal_extra,
                use_days=row.use_days,
                accrual_days=extra_days,
                sort_order=2,
            )
        )
    total = sum(part.nominal for part in parts)
    return IssueDraft(
        full_name=row.full_name,
        login="",
        password="",
        nominal=total,
        balance=None,
        note=row.note,
        expires_on=None,
        segment_id=employee.id,
        template_id=template.id,
        color=row.color,
        department=row.department,
        job_title=row.job_title,
        phone=row.phone,
        discount_percent=row.discount_percent,
        segment_name=employee.name,
        single_use=False,
        use_days=row.use_days,
        accrual_days=parts[0].accrual_days,
        ledger_sheet=LedgerSheet.employee.value,
        company=row.company,
        birth_date=None,
        show_wallet_buttons=False,
        template_name=template.name,
        parts=tuple(parts),
    )


def _parse_nominal(raw: object, label: str) -> int:
    try:
        value = parse_money(raw, blank_as_none=True)
    except MoneyError as exc:
        raise BonusError(f"{label}: {exc.message}") from exc
    if value is None:
        raise BonusError(f"{label}: укажите число")
    if value < 0:
        raise BonusError(f"{label}: сумма не может быть отрицательной")
    return value


def _parse_discount(raw: object) -> int:
    text = str(raw or "").strip().replace("%", "").replace(" ", "")
    try:
        return normalize_discount(int(float(text.replace(",", "."))))
    except (TypeError, ValueError, BonusError) as exc:
        raise BonusError("Процент на карточке: целое число от 0 до 100") from exc


def _clean_text(raw: object, limit: int) -> str:
    text = str(raw or "")
    text = "".join(ch for ch in text if ch >= " " or ch in "\t")
    return " ".join(text.split())[:limit]


def _clean_phone(raw: object) -> str:
    text = str(raw or "").strip()
    text = "".join(ch for ch in text if ch >= " ")
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
    path = get_settings().data_path() / "employee_import"
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


def _save_batch(preview: EmployeeImportPreview, catalog: dict[str, Any]) -> None:
    payload = {
        "batch_id": preview.batch_id,
        "filename": preview.filename,
        "kind": preview.kind,
        "kind_title": preview.kind_title,
        "total_rows": preview.total_rows,
        "ok_rows": preview.ok_rows,
        "error_rows": preview.error_rows,
        "duplicate_rows": preview.duplicate_rows,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "template_id": catalog["template"].id,
        "employee_segment_id": catalog["employee"].id,
        "extra_segment_id": None
        if catalog["extra"] is None
        else catalog["extra"].id,
        "rows": [asdict(row) for row in preview.rows],
    }
    path = _batch_dir() / f"{preview.batch_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _clear_batch(batch_id: str) -> None:
    path = _batch_dir() / f"{re.sub(r'[^A-Za-z0-9_-]', '', batch_id)}.json"
    if path.is_file():
        path.unlink()


def save_employee_import_report(batch_id: str, payload: bytes) -> str:
    """Сохраняет xlsx-отчёт и возвращает id для скачивания."""
    report_id = re.sub(r"[^A-Za-z0-9_-]", "", batch_id)[:40] or secrets.token_urlsafe(8)
    path = _batch_dir() / f"report_{report_id}.xlsx"
    path.write_bytes(payload)
    return report_id


def load_employee_import_report(report_id: str) -> bytes:
    """Читает сохранённый отчёт."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", report_id)[:64]
    path = _batch_dir() / f"report_{safe}.xlsx"
    if not path.is_file():
        raise BonusError("Отчёт не найден")
    return path.read_bytes()

"""Пакетная выгрузка ссылок на карты для рассылки по ресторанам/ООО."""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal
from urllib.parse import urlencode

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bonus.ledger import LEDGER_CHOICES, parse_ledger_sheet
from bonus.models import (
    Certificate,
    CertificateStatus,
    LinkDelivery,
    Segment,
    User,
)
from bonus.values import format_date, format_money_plain, today_msk

GroupBy = Literal["restaurant", "company", "segment", "none"]
DeliveryFilter = Literal["undelivered", "delivered", "all"]
CardStatusFilter = Literal["active", "all"]

DEFAULT_MESSAGE_TEMPLATE = (
    "{Имя}, для вас выпущен сертификат {Тип} на {Номинал} ₽, "
    "действует до {Срок}. Открыть карту: {Ссылка}"
)
_PAGE_SIZE = 50
_WARN_LINE = "ссылку не публикуйте в общих чатах — по ней можно потратить баланс"
_SHEET_BAD = re.compile(r'[\[\]:*?/\\]')
_FILE_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class ExportFilters:
    """Фильтры страницы рассылки (GET-параметры)."""

    ledger: tuple[str, ...] = ()
    segment_ids: tuple[int, ...] = ()
    companies: tuple[str, ...] = ()
    restaurants: tuple[str, ...] = ()
    template_ids: tuple[int, ...] = ()
    card_status: CardStatusFilter = "active"
    delivery: DeliveryFilter = "undelivered"
    issued_from: date | None = None
    issued_to: date | None = None
    expires_from: date | None = None
    expires_to: date | None = None
    query: str = ""
    group_by: GroupBy = "restaurant"
    page: int = 1
    message_template: str = DEFAULT_MESSAGE_TEMPLATE


@dataclass(frozen=True)
class ExportRow:
    """Одна карта в выборке рассылки."""

    certificate_id: int
    public_number: str
    token: str
    full_name: str
    job_title: str
    company: str
    company_key: str
    restaurant: str
    restaurant_key: str
    segment_name: str
    segment_id: int | None
    ledger_sheet: str
    template_id: int | None
    nominal: int
    balance: int
    expires_on: date | None
    created_at: datetime | None
    delivered: bool
    delivery_group: str


@dataclass(frozen=True)
class FilterOption:
    """Пункт мультивыбора с частотным написанием."""

    key: str
    label: str
    count: int


@dataclass(frozen=True)
class ExportGroup:
    """Группа карт для предпросмотра и файла."""

    key: str
    label: str
    rows: tuple[ExportRow, ...]


@dataclass(frozen=True)
class ExportPreview:
    """Данные страницы предпросмотра."""

    rows: tuple[ExportRow, ...]
    groups: tuple[ExportGroup, ...]
    total: int
    page: int
    page_size: int
    page_rows: tuple[ExportRow, ...]
    company_options: tuple[FilterOption, ...]
    restaurant_options: tuple[FilterOption, ...]


def normalize_place_key(value: str) -> str:
    """Ключ сравнения ООО/ресторана: trim, lower, ё→е, дефис/пробелы схлопываются."""
    text = (value or "").strip().casefold().replace("ё", "е")
    text = text.replace("-", " ").replace("‑", " ").replace("–", " ").replace("—", " ")
    text = " ".join(text.split())
    # «МС» и «М-С», «Жарь птица» и «ЖАРЬ-ПТИЦА» → один ключ
    return text.replace(" ", "")


def display_place_label(raw_values: list[str]) -> str:
    """Самое частое написание среди сырых значений."""
    cleaned = [(item or "").strip() or "—" for item in raw_values]
    if not cleaned:
        return "—"
    return Counter(cleaned).most_common(1)[0][0]


def parse_export_filters(
    *,
    ledger: list[str] | None = None,
    segment: list[str] | None = None,
    company: list[str] | None = None,
    restaurant: list[str] | None = None,
    template: list[str] | None = None,
    card_status: str = "active",
    delivery: str = "undelivered",
    issued_from: str = "",
    issued_to: str = "",
    expires_from: str = "",
    expires_to: str = "",
    q: str = "",
    group_by: str = "restaurant",
    page: int = 1,
    tpl: str = "",
) -> ExportFilters:
    """Собирает фильтры из query-параметров."""
    status: CardStatusFilter = "all" if card_status.strip() == "all" else "active"
    delivery_filter: DeliveryFilter = "all"
    cleaned_delivery = delivery.strip().casefold()
    if cleaned_delivery in {"undelivered", "delivered", "all"}:
        delivery_filter = cleaned_delivery  # type: ignore[assignment]
    else:
        delivery_filter = "undelivered"
    group: GroupBy = "restaurant"
    if group_by.strip() in {"restaurant", "company", "segment", "none"}:
        group = group_by.strip()  # type: ignore[assignment]
    segment_ids = tuple(_parse_ints(segment or []))
    template_ids = tuple(_parse_ints(template or []))
    ledgers = tuple(
        item.strip()
        for item in (ledger or [])
        if item.strip() and _safe_ledger(item.strip())
    )
    return ExportFilters(
        ledger=ledgers,
        segment_ids=segment_ids,
        companies=tuple(normalize_place_key(item) for item in (company or []) if item.strip()),
        restaurants=tuple(
            normalize_place_key(item) for item in (restaurant or []) if item.strip()
        ),
        template_ids=template_ids,
        card_status=status,
        delivery=delivery_filter,
        issued_from=_parse_optional_date(issued_from),
        issued_to=_parse_optional_date(issued_to),
        expires_from=_parse_optional_date(expires_from),
        expires_to=_parse_optional_date(expires_to),
        query=(q or "").strip(),
        group_by=group,
        page=max(1, int(page or 1)),
        message_template=(tpl or "").strip() or DEFAULT_MESSAGE_TEMPLATE,
    )


def filters_query(filters: ExportFilters, *, extra: dict[str, str] | None = None) -> str:
    """Собирает query-string для ссылок скачивания/пагинации."""
    params: list[tuple[str, str]] = []
    for item in filters.ledger:
        params.append(("ledger", item))
    for item in filters.segment_ids:
        params.append(("segment", str(item)))
    for item in filters.companies:
        params.append(("company", item))
    for item in filters.restaurants:
        params.append(("restaurant", item))
    for item in filters.template_ids:
        params.append(("template", str(item)))
    params.append(("card_status", filters.card_status))
    params.append(("delivery", filters.delivery))
    if filters.issued_from:
        params.append(("issued_from", filters.issued_from.isoformat()))
    if filters.issued_to:
        params.append(("issued_to", filters.issued_to.isoformat()))
    if filters.expires_from:
        params.append(("expires_from", filters.expires_from.isoformat()))
    if filters.expires_to:
        params.append(("expires_to", filters.expires_to.isoformat()))
    if filters.query:
        params.append(("q", filters.query))
    params.append(("group_by", filters.group_by))
    if filters.message_template != DEFAULT_MESSAGE_TEMPLATE:
        params.append(("tpl", filters.message_template))
    if extra:
        for key, value in extra.items():
            params.append((key, value))
    return urlencode(params)


def load_export_rows(db: Session, filters: ExportFilters, *, base_url: str) -> list[ExportRow]:
    """Один запрос с join-ами: карты под фильтры рассылки."""
    del base_url  # ссылка строится позже через token
    today = today_msk()
    statement = (
        select(Certificate, User, Segment, LinkDelivery)
        .join(User, Certificate.holder_id == User.id)
        .outerjoin(Segment, Certificate.segment_id == Segment.id)
        .outerjoin(LinkDelivery, LinkDelivery.certificate_id == Certificate.id)
        .order_by(Certificate.id.asc())
    )
    if filters.card_status == "active":
        statement = statement.where(
            Certificate.status == CertificateStatus.active,
            Certificate.balance > 0,
            or_(Certificate.expires_on.is_(None), Certificate.expires_on >= today),
        )
    if filters.ledger:
        statement = statement.where(Certificate.ledger_sheet.in_(filters.ledger))
    if filters.segment_ids:
        statement = statement.where(Certificate.segment_id.in_(filters.segment_ids))
    if filters.template_ids:
        statement = statement.where(Certificate.template_id.in_(filters.template_ids))
    if filters.issued_from is not None:
        start = datetime(
            filters.issued_from.year,
            filters.issued_from.month,
            filters.issued_from.day,
            tzinfo=timezone.utc,
        )
        statement = statement.where(Certificate.created_at >= start)
    if filters.issued_to is not None:
        end = datetime(
            filters.issued_to.year,
            filters.issued_to.month,
            filters.issued_to.day,
            23,
            59,
            59,
            tzinfo=timezone.utc,
        )
        statement = statement.where(Certificate.created_at <= end)
    if filters.expires_from is not None:
        statement = statement.where(
            Certificate.expires_on.is_not(None),
            Certificate.expires_on >= filters.expires_from,
        )
    if filters.expires_to is not None:
        statement = statement.where(
            Certificate.expires_on.is_not(None),
            Certificate.expires_on <= filters.expires_to,
        )
    if filters.delivery == "undelivered":
        statement = statement.where(LinkDelivery.id.is_(None))
    elif filters.delivery == "delivered":
        statement = statement.where(LinkDelivery.id.is_not(None))

    rows = db.execute(statement).all()
    found: list[ExportRow] = []
    needle = filters.query.casefold()
    company_keys = set(filters.companies)
    restaurant_keys = set(filters.restaurants)
    for certificate, holder, segment, delivery in rows:
        company = (certificate.company or "").strip()
        restaurant = (holder.department or "").strip()
        company_key = normalize_place_key(company)
        restaurant_key = normalize_place_key(restaurant)
        if company_keys and company_key not in company_keys:
            continue
        if restaurant_keys and restaurant_key not in restaurant_keys:
            continue
        segment_name = (segment.name if segment is not None else "") or ""
        if needle:
            haystack = " ".join(
                (
                    holder.full_name,
                    certificate.public_number,
                    company,
                    restaurant,
                    segment_name,
                    holder.job_title or "",
                )
            ).casefold()
            if needle not in haystack:
                continue
        found.append(
            ExportRow(
                certificate_id=certificate.id,
                public_number=certificate.public_number,
                token=certificate.token,
                full_name=holder.full_name.strip(),
                job_title=(holder.job_title or "").strip(),
                company=company or "—",
                company_key=company_key or normalize_place_key("—"),
                restaurant=restaurant or "—",
                restaurant_key=restaurant_key or normalize_place_key("—"),
                segment_name=segment_name or "—",
                segment_id=certificate.segment_id,
                ledger_sheet=certificate.ledger_sheet,
                template_id=certificate.template_id,
                nominal=certificate.nominal,
                balance=certificate.balance,
                expires_on=certificate.expires_on,
                created_at=certificate.created_at,
                delivered=delivery is not None,
                delivery_group="" if delivery is None else (delivery.group_label or ""),
            )
        )
    return found


def build_preview(
    rows: list[ExportRow],
    filters: ExportFilters,
    *,
    company_options: tuple[FilterOption, ...] = (),
    restaurant_options: tuple[FilterOption, ...] = (),
) -> ExportPreview:
    """Группы и страница предпросмотра; опции ООО/ресторанов можно передать снаружи."""
    groups = group_rows(rows, filters.group_by)
    total = len(rows)
    page = min(filters.page, max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE) if total else 1)
    start = (page - 1) * _PAGE_SIZE
    page_rows = tuple(rows[start : start + _PAGE_SIZE])
    if not company_options:
        company_options = tuple(
            _place_options([row.company for row in rows], [row.company_key for row in rows])
        )
    if not restaurant_options:
        restaurant_options = tuple(
            _place_options([row.restaurant for row in rows], [row.restaurant_key for row in rows])
        )
    return ExportPreview(
        rows=tuple(rows),
        groups=tuple(groups),
        total=total,
        page=page,
        page_size=_PAGE_SIZE,
        page_rows=page_rows,
        company_options=company_options,
        restaurant_options=restaurant_options,
    )


def load_place_facets(db: Session) -> tuple[tuple[FilterOption, ...], tuple[FilterOption, ...]]:
    """ООО и рестораны по активным картам с остатком (для мультивыбора)."""
    today = today_msk()
    rows = db.execute(
        select(Certificate.company, User.department)
        .join(User, Certificate.holder_id == User.id)
        .where(
            Certificate.status == CertificateStatus.active,
            Certificate.balance > 0,
            or_(Certificate.expires_on.is_(None), Certificate.expires_on >= today),
        )
    ).all()
    companies = [(item[0] or "").strip() or "—" for item in rows]
    restaurants = [(item[1] or "").strip() or "—" for item in rows]
    return (
        tuple(_place_options(companies, [normalize_place_key(item) for item in companies])),
        tuple(_place_options(restaurants, [normalize_place_key(item) for item in restaurants])),
    )


def group_rows(rows: list[ExportRow], group_by: GroupBy) -> list[ExportGroup]:
    """Разбивает выборку по ресторану / ООО / сегменту / без разбивки."""
    if group_by == "none":
        return [ExportGroup(key="all", label="Все карты", rows=tuple(rows))]
    buckets: dict[str, list[ExportRow]] = defaultdict(list)
    labels: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if group_by == "company":
            key = row.company_key
            labels[key].append(row.company)
        elif group_by == "segment":
            key = normalize_place_key(row.segment_name)
            labels[key].append(row.segment_name)
        else:
            key = row.restaurant_key
            labels[key].append(row.restaurant)
        buckets[key].append(row)
    groups: list[ExportGroup] = []
    for key in sorted(buckets.keys(), key=lambda item: display_place_label(labels[item]).casefold()):
        label = display_place_label(labels[key])
        groups.append(ExportGroup(key=key, label=label, rows=tuple(buckets[key])))
    return groups


def card_link(base_url: str, token: str) -> str:
    """Публичная ссылка на карту."""
    return f"{base_url.rstrip('/')}/c/{token}"


def build_message(template: str, row: ExportRow, link: str) -> str:
    """Текст для отправки по шаблону; для бессрочных убирает фразу про срок."""
    text = template or DEFAULT_MESSAGE_TEMPLATE
    replacements = {
        "{Имя}": _first_name(row.full_name),
        "{Тип}": row.segment_name,
        "{Номинал}": format_money_plain(row.nominal),
        "{Срок}": format_date(row.expires_on) if row.expires_on else "",
        "{Ссылка}": link,
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    if row.expires_on is None:
        text = _strip_expiry_phrase(text)
    return " ".join(text.split())


def build_workbook_bytes(
    groups: list[ExportGroup],
    *,
    base_url: str,
    template: str,
    export_date: date | None = None,
) -> bytes:
    """Одна книга: лист на группу + «Сводка»."""
    workbook = Workbook()
    summary = workbook.active
    assert summary is not None
    summary.title = "Сводка"
    summary.append(["Группа", "Карт"])
    for group in groups:
        summary.append([_excel_text(group.label), len(group.rows)])
    _style_table(summary, headers=1, columns=2)
    used_titles: set[str] = {"сводка"}
    for group in groups:
        title = _unique_sheet_title(group.label, used_titles)
        sheet = workbook.create_sheet(title)
        _fill_group_sheet(sheet, group, base_url=base_url, template=template, export_date=export_date)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_zip_bytes(
    groups: list[ExportGroup],
    *,
    base_url: str,
    template: str,
    export_date: date | None = None,
) -> bytes:
    """ZIP: отдельный xlsx на каждую группу."""
    day = export_date or today_msk()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for group in groups:
            book = Workbook()
            sheet = book.active
            assert sheet is not None
            sheet.title = _sheet_title(group.label)
            _fill_group_sheet(
                sheet, group, base_url=base_url, template=template, export_date=day
            )
            file_buffer = io.BytesIO()
            book.save(file_buffer)
            filename = f"Рассылка_{_file_slug(group.label)}_{day.isoformat()}.xlsx"
            archive.writestr(filename, file_buffer.getvalue())
    return buffer.getvalue()


def mark_delivered(
    db: Session,
    certificate_ids: list[int],
    *,
    actor: User,
    group_label: str = "",
) -> int:
    """Отмечает карты как отправленные (upsert по certificate_id)."""
    unique_ids = sorted({int(item) for item in certificate_ids if item})
    if not unique_ids:
        return 0
    existing = {
        row.certificate_id: row
        for row in db.scalars(
            select(LinkDelivery).where(LinkDelivery.certificate_id.in_(unique_ids))
        ).all()
    }
    now = datetime.now(timezone.utc)
    touched = 0
    label = (group_label or "").strip()[:200]
    for certificate_id in unique_ids:
        row = existing.get(certificate_id)
        if row is None:
            db.add(
                LinkDelivery(
                    certificate_id=certificate_id,
                    group_label=label,
                    marked_by_id=actor.id,
                    marked_at=now,
                )
            )
            touched += 1
            continue
        row.group_label = label or row.group_label
        row.marked_by_id = actor.id
        row.marked_at = now
        touched += 1
    db.commit()
    return touched


def unmark_delivered(db: Session, certificate_ids: list[int]) -> int:
    """Снимает отметки рассылки с карт."""
    unique_ids = sorted({int(item) for item in certificate_ids if item})
    if not unique_ids:
        return 0
    rows = list(
        db.scalars(select(LinkDelivery).where(LinkDelivery.certificate_id.in_(unique_ids))).all()
    )
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


def ledger_filter_choices() -> tuple[tuple[str, str], ...]:
    """Варианты листа учёта для фильтра."""
    return LEDGER_CHOICES


def _fill_group_sheet(
    sheet: Worksheet,
    group: ExportGroup,
    *,
    base_url: str,
    template: str,
    export_date: date | None,
) -> None:
    day = export_date or today_msk()
    sheet.append([_excel_text(group.label)])
    sheet.append([f"Дата выгрузки: {format_date(day)}"])
    sheet.append([f"Карт: {len(group.rows)}"])
    sheet.append([_WARN_LINE])
    sheet.append([])
    headers = [
        "№",
        "ФИО",
        "Должность",
        "Тип карты",
        "Номинал",
        "Действует до",
        "Ссылка",
        "Текст для отправки",
    ]
    sheet.append(headers)
    header_row = 6
    for row in group.rows:
        link = card_link(base_url, row.token)
        message = build_message(template, row, link)
        values = [
            _excel_text(row.public_number),
            _excel_text(row.full_name),
            _excel_text(row.job_title),
            _excel_text(row.segment_name),
            row.nominal / 100,
            row.expires_on,
            link,
            _excel_text(message),
        ]
        sheet.append(values)
        excel_row = sheet.max_row
        cell = sheet.cell(excel_row, 7)
        cell.hyperlink = link
        cell.font = Font(color="039980", underline="single")
        if row.expires_on is not None:
            sheet.cell(excel_row, 6).number_format = "DD.MM.YYYY"
        sheet.cell(excel_row, 5).number_format = "#,##0.00"
    _style_table(sheet, headers=header_row, columns=len(headers), freeze_row=header_row)
    widths = (14, 28, 18, 22, 12, 14, 40, 55)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def _style_table(sheet: Worksheet, *, headers: int, columns: int, freeze_row: int | None = None) -> None:
    sheet.freeze_panes = f"A{(freeze_row or headers) + 1}"
    last_col = get_column_letter(columns)
    sheet.auto_filter.ref = f"A{headers}:{last_col}{max(sheet.max_row, headers)}"
    for col in range(1, columns + 1):
        sheet.cell(headers, col).font = Font(bold=True)


def _place_options(raw_values: list[str], keys: list[str]) -> list[FilterOption]:
    by_key: dict[str, list[str]] = defaultdict(list)
    for raw, key in zip(raw_values, keys, strict=False):
        by_key[key].append(raw)
    options: list[FilterOption] = []
    for key, values in by_key.items():
        label = display_place_label(values)
        options.append(FilterOption(key=key, label=f"{label} ({len(values)})", count=len(values)))
    options.sort(key=lambda item: (-item.count, item.label.casefold()))
    return options


def _first_name(full_name: str) -> str:
    parts = full_name.strip().split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return parts[1] if len(parts) >= 2 else parts[0]


def _strip_expiry_phrase(text: str) -> str:
    """Убирает куски про срок у бессрочных карт."""
    cleaned = re.sub(
        r",?\s*действует до\s*[^\s.]*\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.replace("  ", " ").replace(" .", ".").strip(" ,")
    return cleaned


def _excel_text(value: str) -> str:
    """Экранирует значения, начинающиеся с = + - @."""
    text = value or ""
    if text and text[0] in "=+-@":
        return "'" + text
    return text


def _sheet_title(label: str) -> str:
    cleaned = _SHEET_BAD.sub(" ", (label or "Группа").strip()) or "Группа"
    cleaned = " ".join(cleaned.split())
    return cleaned[:31]


def _unique_sheet_title(label: str, used: set[str]) -> str:
    base = _sheet_title(label)
    candidate = base
    index = 2
    while candidate.casefold() in used:
        suffix = f" {index}"
        candidate = (base[: 31 - len(suffix)] + suffix).strip()
        index += 1
    used.add(candidate.casefold())
    return candidate


def _file_slug(label: str) -> str:
    cleaned = _FILE_BAD.sub("_", (label or "группа").strip()) or "группа"
    cleaned = " ".join(cleaned.split())
    return cleaned[:80]


def _parse_ints(values: list[str]) -> list[int]:
    result: list[int] = []
    for item in values:
        cleaned = item.strip()
        if not cleaned:
            continue
        try:
            result.append(int(cleaned))
        except ValueError:
            continue
    return result


def _parse_optional_date(raw: str) -> date | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def _safe_ledger(value: str) -> bool:
    try:
        parse_ledger_sheet(value)
    except ValueError:
        return False
    return True

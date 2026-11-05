"""Полная выгрузка всех карт в Excel для главного администратора."""

from __future__ import annotations

import io
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from bonus.export_mail import card_link
from bonus.models import Certificate, User
from bonus.present import load_card
from bonus.values import format_date, format_money_plain, today_msk

_EXPORT_HEADERS: tuple[str, ...] = (
    "ID карты",
    "Публичный номер",
    "Код карты",
    "ФИО",
    "Логин",
    "Отдел",
    "Должность",
    "Телефон",
    "Ресторан (профиль)",
    "ООО",
    "Дата рождения",
    "Лист учёта",
    "Сегмент / шаблон",
    "Номинал, ₽",
    "Остаток, ₽",
    "Статус",
    "Комментарий",
    "Срок действия",
    "Дата выпуска",
    "Цвет",
    "Скидка, %",
    "Разовая",
    "Дни использования",
    "Код при списании",
    "Комбо",
    "Части карты",
    "Распаковка",
    "Ожидает распаковки",
    "Подарок открыт",
    "Можно тратить",
    "Спасибо user id",
    "Ссылка на карту",
)


def build_all_cards_workbook(db: Session, *, base_url: str) -> bytes:
    """Собирает XLSX со всеми неудалёнными картами и ссылками."""
    root = (base_url or "").rstrip("/")
    certificates = list(
        db.scalars(
            select(Certificate)
            .where(Certificate.is_deleted.is_(False))
            .options(
                selectinload(Certificate.holder),
                selectinload(Certificate.segment),
                selectinload(Certificate.template),
                selectinload(Certificate.parts),
            )
            .order_by(Certificate.id.asc())
        ).all()
    )
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Карты"
    sheet.append(list(_EXPORT_HEADERS))
    for certificate in certificates:
        view = load_card(db, certificate, root, None)
        holder = certificate.holder
        link = card_link(root, view.token)
        parts_text = _format_parts(view.parts)
        sheet.append(
            [
                certificate.id,
                _excel_safe(view.public_number),
                _excel_safe(view.barcode_value),
                _excel_safe(view.holder_name),
                _excel_safe(view.holder_login),
                _excel_safe(view.department),
                _excel_safe(view.job_title),
                _excel_safe(view.phone),
                _excel_safe(_holder_restaurant(holder)),
                _excel_safe(view.company),
                _format_optional_date(view.birth_date),
                _excel_safe(view.ledger_sheet),
                _excel_safe(view.segment_name),
                format_money_plain(view.nominal),
                format_money_plain(view.balance),
                _excel_safe(view.status.value if hasattr(view.status, "value") else str(view.status)),
                _excel_safe(view.note),
                _format_optional_date(view.expires_on),
                _format_datetime(certificate.created_at),
                _excel_safe(view.color),
                view.discount_percent,
                _yes_no(view.single_use),
                _excel_safe(view.use_days_label),
                _yes_no(view.require_debit_code),
                _yes_no(view.is_combo),
                _excel_safe(parts_text),
                _excel_safe(view.unwrap_kind),
                _yes_no(view.unwrap_pending),
                _yes_no(view.gift_opened),
                _yes_no(view.can_spend),
                _excel_safe((holder.spasibo_user_id or "") if holder else ""),
                _excel_safe(link),
            ]
        )
        link_cell = sheet.cell(sheet.max_row, len(_EXPORT_HEADERS))
        link_cell.hyperlink = link
    for index, width in enumerate(_column_widths(), start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_filename() -> str:
    """Имя файла с датой выгрузки."""
    day = today_msk().strftime("%Y-%m-%d")
    return f"teams_bonus_karty_{day}.xlsx"


def _format_parts(parts: tuple[object, ...]) -> str:
    if not parts:
        return ""
    chunks: list[str] = []
    for part in parts:
        segment = getattr(part, "segment_name", "")
        balance = format_money_plain(getattr(part, "balance", 0))
        nominal = format_money_plain(getattr(part, "nominal", 0))
        chunks.append(f"{segment}: {balance}/{nominal}")
    return "; ".join(chunks)


def _holder_restaurant(holder: User | None) -> str:
    if holder is None:
        return ""
    return (holder.restaurant or "").strip()


def _format_optional_date(value: date | None) -> str:
    if value is None:
        return ""
    return format_date(value)


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%Y-%m-%d %H:%M")


def _yes_no(value: bool) -> str:
    return "да" if value else "нет"


def _excel_safe(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith("="):
        return f"'{text}"
    return text


def _column_widths() -> tuple[int, ...]:
    return (
        10,
        14,
        12,
        28,
        16,
        16,
        18,
        14,
        18,
        20,
        12,
        14,
        22,
        12,
        12,
        10,
        32,
        12,
        18,
        10,
        8,
        8,
        18,
        12,
        8,
        36,
        12,
        10,
        10,
        10,
        14,
        48,
    )

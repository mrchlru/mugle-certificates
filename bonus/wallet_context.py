"""Данные pass из тех же источников, что и страница карты."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from bonus.catalog import template_logo_is_custom
from bonus.models import CardTemplate, Certificate, User


@dataclass(frozen=True)
class WalletCardContext:
    """Визуал и подписи для WalletWallet без дублирования логики карточки."""

    color: str
    segment_label: str
    strip_source: str
    logo_source: str
    logo_custom: bool
    discount_percent: int


def resolve_wallet_card_context(db: Session, certificate: Certificate) -> WalletCardContext:
    """Цвет, баннер и логотип карты (шаблон → поля certificate → дефолт)."""
    template = _resolve_template(db, certificate)
    color = (getattr(certificate, "color", None) or "").strip()
    if not color and template is not None:
        color = (template.color or "").strip()
    if not color:
        color = "#00A8A8"
    strip_source = ""
    logo_source = ""
    logo_custom = False
    discount = int(getattr(certificate, "discount_percent", 0) or 0)
    if template is not None:
        strip_source = (template.image_path or "").strip()
        logo_custom = template_logo_is_custom(template)
        if logo_custom:
            logo_source = (template.logo_path or "").strip()
        if template.discount_percent:
            discount = int(template.discount_percent)
    segment_label = _segment_label(db, certificate, template)
    return WalletCardContext(
        color=color[:32],
        segment_label=segment_label,
        strip_source=strip_source,
        logo_source=logo_source,
        logo_custom=logo_custom,
        discount_percent=discount,
    )


def wallet_holder_display_name(holder: User | None) -> str:
    """ФИО без отчества; при нехватке места — «Фамилия И.»"""
    full = (holder.full_name if holder is not None else "") or ""
    parts = [part for part in full.split() if part.strip()]
    if not parts:
        return ""
    if len(parts) >= 3:
        label = f"{parts[0]} {parts[1]}"
    elif len(parts) == 2:
        label = f"{parts[0]} {parts[1]}"
    else:
        label = parts[0]
    if len(label) > 34 and len(parts) >= 2:
        label = f"{parts[0]} {parts[1][0]}."
    return label[:40]


def _resolve_template(db: Session, certificate: Certificate) -> CardTemplate | None:
    template = getattr(certificate, "template", None)
    if template is not None:
        return template
    template_id = getattr(certificate, "template_id", None)
    if template_id is None:
        return None
    return db.get(CardTemplate, template_id)


def _segment_label(
    db: Session,
    certificate: Certificate,
    template: CardTemplate | None,
) -> str:
    if template is not None and (template.name or "").strip():
        return str(template.name).strip()
    segment = getattr(certificate, "segment", None)
    if segment is not None and getattr(segment, "name", None):
        return str(segment.name).strip()
    segment_id = getattr(certificate, "segment_id", None)
    if segment_id is None:
        return ""
    from bonus.models import Segment

    row = db.get(Segment, segment_id)
    return (row.name or "").strip() if row is not None else ""

"""Эталон старой сводки (полный CardView) — только для bench и паритет-теста.

Не импортировать из боевого кода.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bonus.models import Certificate, CertificateStatus, CardTemplate, User
from bonus.present import (
    SUMMARY_PAGE_SIZE,
    CertificateSearchPage,
    _card_view,
    _segment_name,
    _sort_cards,
    normalize_sort,
    normalize_status_filter,
)
from bonus.services import apply_due_expiries
from bonus.values import today_msk


def _row_display_label(db: Session, certificate: Certificate) -> str:
    """Имя шаблона (как на карточке), иначе сегмент — в строке поиска и эталоне."""
    if certificate.template_id:
        template = db.get(CardTemplate, certificate.template_id)
        if template is not None:
            name = (template.name or "").strip()
            if name:
                return name
    return _segment_name(db, certificate.segment_id)


def search_certificates_full(
    db: Session,
    query: str,
    template_id: int | None = None,
    *,
    sort: str = "name",
    status: str = "all",
    offset: int = 0,
    limit: int = SUMMARY_PAGE_SIZE,
) -> CertificateSearchPage:
    """Старая реализация: все карты → CardView → сортировка → срез страницы."""
    apply_due_expiries(db)
    status_key = normalize_status_filter(status)
    today = today_msk()
    statement = (
        select(Certificate, User)
        .join(User, Certificate.holder_id == User.id)
        .where(Certificate.is_deleted.is_(False))
        .order_by(Certificate.id.desc())
    )
    if template_id is not None and template_id > 0:
        statement = statement.where(Certificate.template_id == template_id)
    if status_key == "blocked":
        statement = statement.where(Certificate.status == CertificateStatus.blocked)
    elif status_key == "expired":
        statement = statement.where(
            Certificate.expires_on.is_not(None),
            Certificate.expires_on < today,
        )
    elif status_key == "active":
        statement = statement.where(
            Certificate.status == CertificateStatus.active,
            or_(Certificate.expires_on.is_(None), Certificate.expires_on >= today),
        )
    rows = db.execute(statement).all()
    needle = " ".join(query.split()).casefold()
    found = []
    for certificate, holder in rows:
        row_label = _row_display_label(db, certificate)
        haystack = " ".join(
            (
                holder.full_name,
                holder.department,
                holder.job_title,
                holder.phone,
                certificate.public_number,
                certificate.short_code,
                certificate.note,
                row_label,
            )
        ).casefold()
        if needle and needle not in haystack:
            continue
        found.append(_card_view(db, certificate, holder, "", ()))
    ordered = _sort_cards(found, normalize_sort(sort))
    start = max(0, int(offset))
    page_size = max(1, min(int(limit), 100))
    page = ordered[start : start + page_size]
    return CertificateSearchPage(
        cards=tuple(page),
        total=len(ordered),
        offset=start,
        limit=page_size,
    )

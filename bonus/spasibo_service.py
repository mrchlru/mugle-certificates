"""Сервис API «Спасибо»: карта и начисление по spasibo_user_id."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import Certificate, Role, Transaction, TxType, User
from bonus.present import load_card
from bonus.services import BonusError, credit_certificate
from bonus.spasibo_connect import _certificate_eligible


@dataclass(frozen=True)
class SpasiboCardPayload:
    """Данные карты для приложения «Спасибо»."""

    linked: bool
    spasibo_user_id: str
    public_number: str
    short_code: str
    short_code_display: str
    balance_kopecks: int
    balance_rub: str
    holder_name: str
    card_url: str
    color: str
    template_url: str
    logo_url: str
    barcode_value: str
    discount_percent: int
    status: str


def resolve_certificate_for_spasibo(db: Session, spasibo_user_id: str) -> Certificate | None:
    """Находит employee-карту, привязанную к ID Спасибо."""
    cleaned = _clean_spasibo_id(spasibo_user_id)
    if cleaned is None:
        return None
    holder = db.scalar(
        select(User).where(
            User.spasibo_user_id == cleaned,
            User.role == Role.employee,
            User.is_active.is_(True),
        )
    )
    if holder is None:
        return None
    certificate: Certificate | None = None
    if holder.spasibo_certificate_id:
        certificate = db.get(Certificate, holder.spasibo_certificate_id)
    if certificate is None or certificate.holder_id != holder.id:
        certificate = _pick_primary_certificate(db, holder.id)
    if certificate is None:
        return None
    eligible, _ = _certificate_eligible(db, certificate)
    if not eligible:
        return None
    return certificate


def card_payload_for_spasibo(
    db: Session,
    spasibo_user_id: str,
    *,
    base_url: str,
) -> SpasiboCardPayload:
    """Собирает JSON карты или linked=False."""
    cleaned = _clean_spasibo_id(spasibo_user_id)
    if cleaned is None:
        raise BonusError("Некорректный ID Спасибо")
    certificate = resolve_certificate_for_spasibo(db, cleaned)
    if certificate is None:
        return SpasiboCardPayload(
            linked=False,
            spasibo_user_id=cleaned,
            public_number="",
            short_code="",
            short_code_display="",
            balance_kopecks=0,
            balance_rub="0",
            holder_name="",
            card_url="",
            color="#00A8A8",
            template_url="",
            logo_url="",
            barcode_value="",
            discount_percent=0,
            status="",
        )
    view = load_card(db, certificate, base_url.rstrip("/"), actor=None)
    root = base_url.rstrip("/")
    return SpasiboCardPayload(
        linked=True,
        spasibo_user_id=cleaned,
        public_number=view.public_number,
        short_code=view.short_code,
        short_code_display=view.barcode_value,
        balance_kopecks=view.balance,
        balance_rub=_format_rub(view.balance),
        holder_name=view.holder_name,
        card_url=f"{root}/c/{view.token}",
        color=view.color or "#00A8A8",
        template_url=view.template_url or "",
        logo_url=view.logo_url or "",
        barcode_value=view.barcode_value,
        discount_percent=view.discount_percent or 0,
        status=str(view.status.value if hasattr(view.status, "value") else view.status),
    )


def redeem_bonus_for_spasibo(
    db: Session,
    *,
    spasibo_user_id: str,
    bonus_amount_rub: int,
    idempotency_key: str,
    comment: str = "",
) -> tuple[int, bool]:
    """Начисляет бонусы на связанную карту. Возвращает (balance_after_kopecks, replayed)."""
    cleaned = _clean_spasibo_id(spasibo_user_id)
    if cleaned is None:
        raise BonusError("Некорректный ID Спасибо")
    if bonus_amount_rub <= 0:
        raise BonusError("Сумма бонусов должна быть больше нуля")
    marker = _idempotency_marker(idempotency_key)
    existing = db.scalar(select(Transaction).where(Transaction.order_ref == marker))
    if existing is not None:
        certificate = db.get(Certificate, existing.certificate_id)
        if certificate is None:
            raise BonusError("Карта операции не найдена")
        db.refresh(certificate)
        return certificate.balance, True
    certificate = resolve_certificate_for_spasibo(db, cleaned)
    if certificate is None:
        raise BonusError("Карта Teams не привязана к этому ID Спасибо")
    amount_kopecks = bonus_amount_rub * 100
    text = comment.strip() or "Обмен спасибок (Спасибо → Teams Bonus)"
    credit_certificate(db, certificate, None, amount_kopecks, text, grow_nominal=True)
    tx = db.scalars(
        select(Transaction)
        .where(
            Transaction.certificate_id == certificate.id,
            Transaction.tx_type == TxType.credit,
        )
        .order_by(Transaction.id.desc())
    ).first()
    if tx is not None:
        tx.order_ref = marker
        db.commit()
    db.refresh(certificate)
    return certificate.balance, False


def spasibo_api_key_expected() -> str:
    """Ключ для заголовка X-Spasibo-Key (отдельный или общий с iiko)."""
    settings = get_settings()
    key = (settings.spasibo_api_key or settings.iiko_api_key or "").strip()
    return key


def _pick_primary_certificate(db: Session, holder_id: int) -> Certificate | None:
    rows = db.scalars(
        select(Certificate)
        .where(
            Certificate.holder_id == holder_id,
            Certificate.is_deleted.is_(False),
        )
        .order_by(Certificate.id.desc())
    ).all()
    for certificate in rows:
        eligible, _ = _certificate_eligible(db, certificate)
        if eligible:
            return certificate
    return None


def _clean_spasibo_id(raw: str) -> str | None:
    text = str(raw or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value <= 0:
        return None
    return str(value)


def _format_rub(kopecks: int) -> str:
    whole = max(0, kopecks) // 100
    return f"{whole:,}".replace(",", " ")


def _idempotency_marker(key: str) -> str:
    cleaned = " ".join(str(key or "").split())[:80]
    if cleaned == "":
        raise BonusError("Нужен Idempotency-Key")
    return f"spasibo:{cleaned}"

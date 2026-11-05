"""Сервис API «Спасибо»: карта и начисление по spasibo_user_id."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import Certificate, Role, Transaction, TxType, User
from bonus.partners import partner_cities
from bonus.present import HISTORY_PAGE_SIZE, TxView, load_card, load_history_page
from bonus.services import BonusError, credit_certificate, find_certificate
from bonus.spasibo_connect import _certificate_eligible, _link_conflict
from bonus.values import format_dt, format_money_plain, status_label, tx_label
from bonus.weekdays import WEEKDAY_CHOICES


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
    logo_custom: bool
    default_logo_url: str
    barcode_value: str
    barcode_svg: str
    discount_percent: int
    status: str
    status_label: str
    segment_name: str
    use_days_label: str
    use_days_active: tuple[str, ...]
    expires_on: str
    token: str
    history: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    history_has_more: bool = False
    history_next_before: int | None = None


def link_spasibo_to_teams_card(
    db: Session,
    *,
    spasibo_user_id: str,
    teams_card: str,
    replace_conflicts: bool = True,
) -> str:
    """
    Привязывает users.id «Спасибо» к employee-карте Teams (MR-…, short code или token).

    Пустой teams_card снимает привязку с держателя, у которого был этот ID Спасибо.
    """
    cleaned_id = _clean_spasibo_id(spasibo_user_id)
    if cleaned_id is None:
        raise BonusError("Некорректный ID Спасибо")
    card_ref = str(teams_card or "").strip()
    if card_ref == "":
        holder = db.scalar(select(User).where(User.spasibo_user_id == cleaned_id))
        if holder is not None:
            holder.spasibo_user_id = ""
            holder.spasibo_certificate_id = None
            db.commit()
        return ""
    certificate = find_certificate(db, card_ref)
    if certificate is None:
        raise BonusError("Карта Teams не найдена")
    holder = certificate.holder
    if holder is None or holder.role != Role.employee:
        raise BonusError("Не карта сотрудника")
    eligible, why = _certificate_eligible(db, certificate)
    if not eligible:
        raise BonusError(why or "Карта не подходит для «Спасибо»")
    if not replace_conflicts:
        conflict = _link_conflict(db, cleaned_id, holder.id, certificate.id)
        if conflict:
            raise BonusError(conflict)
    else:
        for other in db.scalars(
            select(User).where(
                User.spasibo_user_id == cleaned_id,
                User.id != holder.id,
            )
        ):
            other.spasibo_user_id = ""
            other.spasibo_certificate_id = None
        for other in db.scalars(
            select(User).where(
                User.spasibo_certificate_id == certificate.id,
                User.id != holder.id,
            )
        ):
            if other.spasibo_user_id != cleaned_id:
                other.spasibo_user_id = ""
                other.spasibo_certificate_id = None
    holder.spasibo_user_id = cleaned_id
    holder.spasibo_certificate_id = certificate.id
    db.commit()
    return certificate.public_number or ""


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
        root = base_url.rstrip("/")
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
            logo_custom=False,
            default_logo_url=f"{root}/static/logo-teams.png",
            barcode_value="",
            barcode_svg="",
            discount_percent=0,
            status="",
            status_label="",
            segment_name="",
            use_days_label="",
            use_days_active=(),
            expires_on="",
            token="",
        )
    view = load_card(db, certificate, base_url.rstrip("/"), actor=None)
    root = base_url.rstrip("/")
    status_text = str(view.status.value if hasattr(view.status, "value") else view.status)
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
        template_url=_absolute_public_url(root, view.template_url or ""),
        logo_url=_absolute_public_url(root, view.logo_url or ""),
        logo_custom=bool(view.logo_custom),
        default_logo_url=f"{root}/static/logo-teams.png",
        barcode_value=view.barcode_value,
        barcode_svg=view.barcode_svg or "",
        discount_percent=view.discount_percent or 0,
        status=status_text,
        status_label=status_label(
            view.status,
            view.balance,
            view.expires_on,
            view.single_use,
        ),
        segment_name=view.segment_name or "",
        use_days_label=view.use_days_label or "",
        use_days_active=view.use_days_active,
        expires_on=view.expires_on.isoformat() if view.expires_on else "",
        token=view.token,
        history=tuple(_tx_to_dict(item) for item in view.history),
        history_has_more=view.history_has_more,
        history_next_before=view.history_next_before,
    )


def history_payload_for_spasibo(
    db: Session,
    spasibo_user_id: str,
    *,
    before_id: int | None = None,
    limit: int = HISTORY_PAGE_SIZE,
) -> dict[str, Any]:
    """Следующая порция истории операций по карте Спасибо."""
    certificate = resolve_certificate_for_spasibo(db, spasibo_user_id)
    if certificate is None:
        raise BonusError("Карта Teams не привязана к этому ID Спасибо")
    page = load_history_page(
        db,
        certificate,
        None,
        limit=min(max(limit, 1), HISTORY_PAGE_SIZE),
        before_id=before_id,
    )
    return {
        "items": [_tx_to_dict(item) for item in page.items],
        "has_more": page.has_more,
        "next_before": page.next_before,
    }


def partners_payload_for_spasibo() -> dict[str, Any]:
    """Каталог партнёров и подписи дней недели (статический JSON)."""
    return {
        "weekday_choices": [{"value": value, "title": title} for value, title in WEEKDAY_CHOICES],
        "cities": [
            {
                "city": city.city,
                "places": [
                    {"name": place.name, "addresses": list(place.addresses)}
                    for place in city.places
                ],
            }
            for city in partner_cities()
        ],
    }


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


def _absolute_public_url(base: str, url: str) -> str:
    """Публичный URL ассета Teams (для «Спасибо» нельзя отдавать /static/...)."""
    cleaned = (url or "").strip()
    if cleaned == "":
        return ""
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return f"{base.rstrip('/')}{cleaned}"


def _tx_to_dict(item: TxView) -> dict[str, Any]:
    """Сериализует операцию для JSON API «Спасибо»."""
    debit_types = {"debit", "migrate", "expire", "burn"}
    show_amount = item.tx_type not in {"block", "unblock"}
    is_minus = item.tx_type in debit_types
    meta_parts = [format_dt(item.created_at)]
    if item.actor_name:
        meta_parts.append(item.actor_name)
    if item.restaurant:
        meta_parts.append(item.restaurant)
    if item.order_ref:
        meta_parts.append(item.order_ref)
    if item.comment:
        meta_parts.append(item.comment)
    return {
        "id": item.id,
        "title": tx_label(item.tx_type, item.reversal_of_id),
        "meta": " · ".join(meta_parts),
        "amount_display": format_money_plain(item.amount) if show_amount else "",
        "is_minus": is_minus,
        "show_amount": show_amount,
        "cheque_detail": item.cheque_detail or "",
        "undone": item.undone,
    }


def _idempotency_marker(key: str) -> str:
    cleaned = " ".join(str(key or "").split())[:80]
    if cleaned == "":
        raise BonusError("Нужен Idempotency-Key")
    return f"spasibo:{cleaned}"

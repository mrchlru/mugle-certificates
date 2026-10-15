"""API для плагина iikoFront: lookup / списание бонусов / возврат.

Плагин после списания сам вешает тип оплаты на заказ в iiko
(как Statix). Сюда уходит снимок чека для журнала HR.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.db import get_db
from bonus.services import (
    BonusError,
    debit_for_iiko,
    lookup_for_iiko,
    refund_for_iiko,
)

router = APIRouter(prefix="/api/iiko", tags=["iiko"])


class LookupBody(BaseModel):
    """Код с QR или короткий код с карточки."""

    code: str = Field(min_length=1, max_length=500)


class LookupResponse(BaseModel):
    """Что касса показывает после скана (без автоскидки)."""

    public_number: str
    short_code: str
    balance: int
    balance_rub: str
    discount_percent: int
    discount_is_display_only: bool = True
    can_spend: bool
    status: str


class DebitBody(BaseModel):
    """Списание бонусов с заказа iiko."""

    code: str = Field(min_length=1, max_length=500)
    amount_rub: str = Field(min_length=1, max_length=32, description="Сумма заказа или часть в рублях")
    transaction_id: str = Field(min_length=1, max_length=80, description="Guid операции iiko")
    restaurant: str = ""
    comment: str = ""
    order_number: str = ""
    table: str = ""
    cheque_detail: str = ""


class DebitResponse(BaseModel):
    """Сколько списали бонусами и сколько доплатить иначе."""

    public_number: str
    paid: int
    paid_rub: str
    remainder: int
    remainder_rub: str
    balance_after: int
    balance_after_rub: str
    our_tx_id: int
    replayed: bool
    discount_percent: int
    discount_is_display_only: bool = True


class RefundBody(BaseModel):
    """Отмена списания по guid операции iiko."""

    transaction_id: str = Field(min_length=1, max_length=80)
    reason: str = ""


class RefundResponse(BaseModel):
    """Подтверждение возврата бонусов."""

    our_tx_id: int
    refunded: int
    refunded_rub: str


class PluginConfigResponse(BaseModel):
    """Удалённые настройки плагина iiko (локально остаются BaseUrl и ключ)."""

    payment_type_name: str
    payment_system_key: str
    payment_system_name: str
    counteragent_user_name: str = ""


def require_iiko_key(x_iiko_key: Annotated[str, Header(alias="X-Iiko-Key")] = "") -> None:
    """Проверяет ключ плагина. Пустой ключ в настройках = API выключен."""
    expected = get_settings().iiko_api_key.strip()
    if expected == "" or x_iiko_key.strip() != expected:
        raise HTTPException(status_code=401, detail="Неверный или не заданный ключ iiko")


@router.get("/plugin-config", response_model=PluginConfigResponse)
def iiko_plugin_config(
    _: None = Depends(require_iiko_key),
) -> PluginConfigResponse:
    """Отдаёт имена типа оплаты/ПС для касс без переустановки DLL."""
    settings = get_settings()
    return PluginConfigResponse(
        payment_type_name=settings.iiko_plugin_payment_type_name.strip() or "Statix Pro Bonus",
        payment_system_key=settings.iiko_plugin_payment_system_key.strip() or "StatixProBonus",
        payment_system_name=settings.iiko_plugin_payment_system_name.strip() or "Statix Pro",
        counteragent_user_name="",
    )


@router.post("/lookup", response_model=LookupResponse)
def iiko_lookup(
    body: LookupBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_iiko_key),
) -> LookupResponse:
    """Скан QR: остаток и отображаемый процент (не применять как скидку iiko)."""
    try:
        certificate = lookup_for_iiko(db, body.code)
    except BonusError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    from bonus.models import CertificateStatus
    from bonus.values import format_money, today_msk

    expired = certificate.expires_on is not None and certificate.expires_on < today_msk()
    can_spend = (
        certificate.status == CertificateStatus.active and certificate.balance > 0 and not expired
    )
    return LookupResponse(
        public_number=certificate.public_number,
        short_code=certificate.short_code,
        balance=certificate.balance,
        balance_rub=format_money(certificate.balance),
        discount_percent=certificate.discount_percent or 0,
        can_spend=can_spend,
        status=certificate.status.value,
    )


@router.post("/debit", response_model=DebitResponse)
def iiko_debit(
    body: DebitBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_iiko_key),
) -> DebitResponse:
    """Списывает бонусы (целиком или часть); remainder — доплата картой/налом."""
    from bonus.values import MoneyError, format_money, parse_money

    try:
        amount = parse_money(body.amount_rub)
        certificate = lookup_for_iiko(db, body.code)
        result = debit_for_iiko(
            db,
            certificate,
            amount=amount,
            external_id=body.transaction_id,
            restaurant=body.restaurant,
            comment=body.comment or "Teams Bonus",
            order_ref=body.order_number or body.table,
            cheque_detail=_cheque_text(body),
        )
    except MoneyError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return DebitResponse(
        public_number=certificate.public_number,
        paid=result.paid,
        paid_rub=format_money(result.paid),
        remainder=result.remainder,
        remainder_rub=format_money(result.remainder),
        balance_after=result.balance_after,
        balance_after_rub=format_money(result.balance_after),
        our_tx_id=result.transaction.id,
        replayed=result.replayed,
        discount_percent=certificate.discount_percent or 0,
    )


@router.post("/refund", response_model=RefundResponse)
def iiko_refund(
    body: RefundBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_iiko_key),
) -> RefundResponse:
    """Возврат бонусов при отмене оплаты на кассе iiko."""
    from bonus.values import format_money

    try:
        tx = refund_for_iiko(db, body.transaction_id, body.reason)
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return RefundResponse(
        our_tx_id=tx.id,
        refunded=tx.amount,
        refunded_rub=format_money(tx.amount),
    )


def _cheque_text(body: DebitBody) -> str:
    """Собирает текст чека из полей плагина."""
    detail = (body.cheque_detail or "").strip()
    if detail:
        return detail[:4000]
    parts: list[str] = []
    if body.order_number.strip():
        parts.append(f"Заказ: {body.order_number.strip()}")
    if body.table.strip():
        parts.append(f"Стол: {body.table.strip()}")
    if body.comment.strip():
        parts.append(body.comment.strip())
    return "\n".join(parts)[:4000]

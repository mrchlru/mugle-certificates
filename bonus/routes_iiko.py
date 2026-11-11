"""API для плагина iikoFront: lookup / списание бонусов / возврат.

Плагин после списания сам вешает тип оплаты на заказ в iiko
(как Statix). Сюда уходит снимок чека для журнала HR.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.db import get_db
from bonus.debit_code import (
    CodeRequiredResponse,
    DebitCodeError,
    cancel_challenge,
    handle_iiko_debit,
    resend_challenge_code,
)
from bonus.iiko_monitor import (
    DEBIT_BLOCKED_MESSAGE,
    terminal_debit_blocked,
    upsert_heartbeat,
)
from bonus.services import (
    BonusError,
    IikoDebitResult,
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
    challenge_id: str = ""
    debit_code: str = Field(default="", max_length=8, description="4 цифры подтверждения")
    terminal_id: str = Field(default="", max_length=80, description="Guid кассы из плагина")


class HeartbeatBody(BaseModel):
    """Сигнал «плагин жив» с кассы."""

    terminal_id: str = Field(min_length=1, max_length=80)
    machine_name: str = ""
    restaurant: str = ""
    plugin_version: str = ""
    front_api: str = ""
    front_version: str | None = None
    features: list[str] | None = None
    license_ok: bool | None = None
    payment_type_bound: bool | None = None
    payment_type_name: str = ""
    config_source: str = ""
    pending_ops: int = 0
    last_error: str | None = None
    last_error_at: str | None = None

    model_config = {"extra": "ignore"}


class ChallengeIdBody(BaseModel):
    """Идентификатор вызова подтверждения."""

    challenge_id: str = Field(min_length=1, max_length=40)


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
        payment_type_name=settings.iiko_plugin_payment_type_name.strip() or "Teams Pro Bonus",
        payment_system_key=settings.iiko_plugin_payment_system_key.strip() or "TeamsProBonus",
        payment_system_name=settings.iiko_plugin_payment_system_name.strip() or "Teams Pro",
        counteragent_user_name="",
    )


@router.post("/heartbeat")
def iiko_heartbeat(
    body: HeartbeatBody,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_iiko_key),
) -> dict[str, bool]:
    """Принимает heartbeat плагина (upsert по terminal_id)."""
    client_ip = request.client.host if request.client else ""
    try:
        upsert_heartbeat(db, body.model_dump(), client_ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


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


@router.post("/debit")
def iiko_debit(
    body: DebitBody,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_iiko_key),
    x_plugin_features: Annotated[str, Header(alias="X-Plugin-Features")] = "",
    x_terminal_id: Annotated[str, Header(alias="X-Terminal-Id")] = "",
) -> Any:
    """Списывает бонусы (целиком или часть); remainder — доплата картой/налом."""
    from bonus.values import MoneyError, format_money, parse_money

    terminal_key = _resolve_terminal_id(body.terminal_id, x_terminal_id)
    if terminal_debit_blocked(db, terminal_id=terminal_key, restaurant=body.restaurant):
        raise HTTPException(status_code=403, detail=DEBIT_BLOCKED_MESSAGE)

    try:
        amount = parse_money(body.amount_rub)
        certificate = lookup_for_iiko(db, body.code)
        outcome = handle_iiko_debit(
            db,
            certificate,
            order_amount=amount,
            external_id=body.transaction_id,
            restaurant=body.restaurant,
            comment=body.comment or "Teams Bonus",
            order_ref=body.order_number or body.table,
            cheque_detail=_cheque_text(body),
            challenge_id=body.challenge_id.strip() or None,
            code=body.debit_code.strip() or None,
            plugin_features=x_plugin_features,
        )
    except MoneyError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except DebitCodeError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    if isinstance(outcome, CodeRequiredResponse):
        return JSONResponse(
            status_code=202,
            content={
                "status": "code_required",
                "challenge_id": outcome.challenge_id,
                "digits": outcome.digits,
                "expires_in_sec": outcome.expires_in_sec,
                "attempts_left": outcome.attempts_left,
                "resend_after_sec": outcome.resend_after_sec,
                "message": outcome.message,
            },
        )

    return _debit_response(certificate, outcome)


@router.post("/debit/code/resend")
def iiko_debit_code_resend(
    body: ChallengeIdBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_iiko_key),
) -> JSONResponse:
    """Повторная отправка push с кодом."""
    try:
        outcome = resend_challenge_code(db, body.challenge_id)
    except DebitCodeError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return JSONResponse(
        status_code=202,
        content={
            "status": "code_required",
            "challenge_id": outcome.challenge_id,
            "digits": outcome.digits,
            "expires_in_sec": outcome.expires_in_sec,
            "attempts_left": outcome.attempts_left,
            "resend_after_sec": outcome.resend_after_sec,
            "message": outcome.message,
        },
    )


@router.post("/debit/code/cancel")
def iiko_debit_code_cancel(
    body: ChallengeIdBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_iiko_key),
) -> dict[str, bool]:
    """Закрывает вызов подтверждения без списания."""
    cancel_challenge(db, body.challenge_id)
    return {"ok": True}


def _debit_response(certificate, result: IikoDebitResult) -> DebitResponse:
    from bonus.values import format_money

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


def _resolve_terminal_id(body_value: str, header_value: str) -> str:
    """Guid кассы: заголовок X-Terminal-Id или поле тела."""
    header = (header_value or "").strip()
    if header:
        return header
    return (body_value or "").strip()


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

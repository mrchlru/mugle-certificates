"""API для приложения «Спасибо»."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from bonus.db import get_db
from bonus.http import public_base
from bonus.services import BonusError
from bonus.spasibo_service import (
    card_payload_for_spasibo,
    redeem_bonus_for_spasibo,
    spasibo_api_key_expected,
)

router = APIRouter(prefix="/api/spasibo", tags=["spasibo"])


class CardResponse(BaseModel):
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


class RedeemBody(BaseModel):
    spasibo_user_id: str = Field(min_length=1, max_length=32)
    bonus_amount_rub: int = Field(gt=0, le=1_000_000)
    comment: str = ""


class RedeemResponse(BaseModel):
    balance_after_kopecks: int
    balance_after_rub: str
    replayed: bool


def require_spasibo_key(
    x_spasibo_key: Annotated[str, Header(alias="X-Spasibo-Key")] = "",
) -> None:
    expected = spasibo_api_key_expected()
    if expected == "" or x_spasibo_key.strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/card", response_model=CardResponse)
def spasibo_card(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_spasibo_key),
):
    """Карта Teams по users.id из «Спасибо»."""
    del _  # auth marker
    try:
        payload = card_payload_for_spasibo(
            db,
            user_id,
            base_url=public_base(request),
        )
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return CardResponse(**payload.__dict__)


@router.post("/redeem", response_model=RedeemResponse)
def spasibo_redeem(
    body: RedeemBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_spasibo_key),
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
):
    """Начисление бонусов после списания спасибок в «Спасибо»."""
    del _
    try:
        balance, replayed = redeem_bonus_for_spasibo(
            db,
            spasibo_user_id=body.spasibo_user_id,
            bonus_amount_rub=body.bonus_amount_rub,
            idempotency_key=idempotency_key,
            comment=body.comment,
        )
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return RedeemResponse(
        balance_after_kopecks=balance,
        balance_after_rub=f"{balance // 100:,}".replace(",", " "),
        replayed=replayed,
    )

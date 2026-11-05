"""API для приложения «Спасибо»."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from bonus.db import get_db
from bonus.http import public_base
from bonus.services import BonusError
from bonus.spasibo_service import (
    card_payload_for_spasibo,
    history_payload_for_spasibo,
    link_spasibo_to_teams_card,
    partners_payload_for_spasibo,
    redeem_bonus_for_spasibo,
    spasibo_api_key_expected,
)

router = APIRouter(prefix="/api/spasibo", tags=["spasibo"])


class HistoryItemResponse(BaseModel):
    id: int
    title: str
    meta: str
    amount_display: str
    is_minus: bool
    show_amount: bool
    cheque_detail: str
    undone: bool


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
    logo_custom: bool = False
    default_logo_url: str = ""
    barcode_value: str
    barcode_svg: str = ""
    discount_percent: int
    status: str
    status_label: str = ""
    segment_name: str = ""
    use_days_label: str = ""
    use_days_active: list[str] = Field(default_factory=list)
    expires_on: str = ""
    token: str = ""
    history: list[HistoryItemResponse] = Field(default_factory=list)
    history_has_more: bool = False
    history_next_before: int | None = None


class HistoryPageResponse(BaseModel):
    items: list[HistoryItemResponse]
    has_more: bool
    next_before: int | None = None


class PartnerPlaceResponse(BaseModel):
    name: str
    addresses: list[str]


class PartnerCityResponse(BaseModel):
    city: str
    places: list[PartnerPlaceResponse]


class WeekdayChoiceResponse(BaseModel):
    value: str
    title: str


class PartnersResponse(BaseModel):
    weekday_choices: list[WeekdayChoiceResponse]
    cities: list[PartnerCityResponse]


class RedeemBody(BaseModel):
    spasibo_user_id: str = Field(min_length=1, max_length=32)
    bonus_amount_rub: int = Field(gt=0, le=1_000_000)
    comment: str = ""


class RedeemResponse(BaseModel):
    balance_after_kopecks: int
    balance_after_rub: str
    replayed: bool


class LinkBody(BaseModel):
    spasibo_user_id: str = Field(min_length=1, max_length=32)
    teams_card: str = Field(max_length=120)
    replace_conflicts: bool = True


class LinkResponse(BaseModel):
    linked: bool
    public_number: str
    spasibo_user_id: str


def require_spasibo_key(
    x_spasibo_key: Annotated[str, Header(alias="X-Spasibo-Key")] = "",
) -> None:
    expected = spasibo_api_key_expected()
    if expected == "" or x_spasibo_key.strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _payload_to_card_response(payload: Any) -> CardResponse:
    data = payload.__dict__
    return CardResponse(**data)


@router.get("/card", response_model=CardResponse)
def spasibo_card(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_spasibo_key),
):
    """Карта Teams по users.id из «Спасибо» (с первой страницей истории)."""
    del _
    try:
        payload = card_payload_for_spasibo(
            db,
            user_id,
            base_url=public_base(request),
        )
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return _payload_to_card_response(payload)


@router.get("/history", response_model=HistoryPageResponse)
def spasibo_history(
    user_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_spasibo_key),
    before: int | None = None,
    limit: int = 10,
):
    """Следующая порция истории (ленивая подгрузка)."""
    del _
    try:
        page = history_payload_for_spasibo(
            db,
            user_id,
            before_id=before,
            limit=limit,
        )
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return HistoryPageResponse(**page)


@router.get("/partners", response_model=PartnersResponse)
def spasibo_partners(
    _: None = Depends(require_spasibo_key),
):
    """Рестораны-партнёры и подписи дней недели."""
    del _
    return PartnersResponse(**partners_payload_for_spasibo())


@router.post("/link", response_model=LinkResponse)
def spasibo_link(
    body: LinkBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_spasibo_key),
):
    """Привязка или отвязка ID «Спасибо» к карте Teams (из админки «Спасибо»)."""
    del _
    try:
        public_number = link_spasibo_to_teams_card(
            db,
            spasibo_user_id=body.spasibo_user_id,
            teams_card=body.teams_card,
            replace_conflicts=body.replace_conflicts,
        )
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    linked = bool(public_number)
    cleaned = str(body.spasibo_user_id).strip()
    return LinkResponse(
        linked=linked,
        public_number=public_number,
        spasibo_user_id=cleaned,
    )


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

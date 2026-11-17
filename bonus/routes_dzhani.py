"""API для продажи сертификатов «Чёрная пятница» ресторана Джани."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.db import get_db
from bonus.http import public_base
from bonus.models import Certificate, CertificateStatus
from bonus.services import BonusError, IssueDraft, issue_certificate
from bonus.values import format_money_plain, format_short_code, parse_date, status_label

router = APIRouter(prefix="/api/dzhani", tags=["dzhani"])
MSK = ZoneInfo("Europe/Moscow")


class IssueBody(BaseModel):
    """Запрос на выпуск подарочного сертификата после оплаты."""

    nominal_rub: int = Field(gt=0, le=1_000_000)
    balance_rub: int | None = Field(default=None, gt=0, le=1_000_000)
    first_name: str = Field(default="", max_length=80)
    last_name: str = Field(default="", max_length=80)
    phone: str = Field(default="", max_length=40)
    birth_date: str = Field(default="", max_length=32)
    contact: str = Field(default="", max_length=200)
    order_id: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=500)
    expires_on: str = Field(default="", max_length=32)
    segment_id: int | None = None
    template_id: int | None = None
    color: str = "#b3122b"
    ledger_sheet: str = "thanks"
    single_use: bool = True


class IssueResponse(BaseModel):
    """Выпущенная карта для витрины Чёрной пятницы."""

    public_number: str
    short_code: str
    short_code_display: str
    token: str
    card_url: str
    balance_rub: int
    balance_display: str
    status: str
    expires_on: str = ""


class CardStatusResponse(BaseModel):
    """Статус карты для синхронизации реестра."""

    public_number: str
    short_code: str
    status: str
    status_label: str
    balance_rub: int
    balance_display: str


def dzhani_api_key_expected() -> str:
    """Ключ для заголовка X-Dzhani-Key."""
    settings = get_settings()
    key = (settings.dzhani_api_key or settings.iiko_api_key or "").strip()
    return key


def require_dzhani_key(
    x_dzhani_key: Annotated[str, Header(alias="X-Dzhani-Key")] = "",
) -> None:
    expected = dzhani_api_key_expected()
    if expected == "" or x_dzhani_key.strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/issue", response_model=IssueResponse)
def issue_dzhani_certificate(
    body: IssueBody,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_dzhani_key),
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
) -> IssueResponse:
    """Выпускает сертификат Чёрной пятницы (идемпотентно по Idempotency-Key)."""
    del _
    if idempotency_key.strip():
        existing = _find_by_idempotency(db, idempotency_key.strip())
        if existing is not None:
            return _to_issue_response(existing, public_base(request))

    settings = get_settings()
    segment_id = body.segment_id or settings.dzhani_default_segment_id
    template_id = body.template_id or settings.dzhani_default_template_id
    expires = _resolve_expires(body.expires_on)
    balance_rub = body.balance_rub if body.balance_rub is not None else body.nominal_rub
    phone = (body.phone or body.contact).strip()
    birth = _parse_expires(body.birth_date)
    full_name = _holder_full_name(body.first_name, body.last_name, phone, body.order_id)
    note = body.note.strip() or f"Чёрная пятница Джани · {body.order_id}".strip(" ·")
    if idempotency_key.strip():
        note = f"{note} · idk:{idempotency_key.strip()}"[:500]

    try:
        issued = issue_certificate(
            db,
            IssueDraft(
                full_name=full_name,
                login="",
                password="",
                nominal=body.nominal_rub * 100,
                balance=balance_rub * 100,
                note=note,
                expires_on=expires,
                segment_id=segment_id,
                template_id=template_id,
                color=(body.color or settings.dzhani_default_color or "#b3122b")[:32],
                department="Джани ресторани",
                job_title="Покупатель Чёрной пятницы",
                phone=phone[:40],
                discount_percent=0,
                single_use=body.single_use,
                segment_name="Чёрная пятница Джани",
                ledger_sheet=(body.ledger_sheet or "thanks").strip()[:32] or "thanks",
                company="Джани",
                birth_date=birth,
            ),
            actor=None,
        )
    except BonusError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    return _to_issue_response(issued.certificate, public_base(request))


@router.get("/card", response_model=CardStatusResponse)
def dzhani_card_status(
    public_number: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_dzhani_key),
) -> CardStatusResponse:
    """Статус сертификата по номеру для синхронизации реестра."""
    del _
    number = public_number.strip()
    cert = db.scalar(
        select(Certificate).where(
            Certificate.public_number == number,
            Certificate.is_deleted.is_(False),
        )
    )
    if cert is None:
        raise HTTPException(status_code=404, detail="Сертификат не найден")
    return CardStatusResponse(
        public_number=cert.public_number,
        short_code=cert.short_code,
        status=cert.status.value if isinstance(cert.status, CertificateStatus) else str(cert.status),
        status_label=status_label(cert.status),
        balance_rub=cert.balance // 100,
        balance_display=format_money_plain(cert.balance),
    )


def _to_issue_response(cert: Certificate, root: str) -> IssueResponse:
    expires = ""
    if cert.expires_on is not None:
        expires = cert.expires_on.isoformat()
    return IssueResponse(
        public_number=cert.public_number,
        short_code=cert.short_code,
        short_code_display=format_short_code(cert.short_code),
        token=cert.token,
        card_url=f"{root.rstrip('/')}/c/{cert.token}",
        balance_rub=cert.balance // 100,
        balance_display=format_money_plain(cert.balance),
        status=cert.status.value if isinstance(cert.status, CertificateStatus) else str(cert.status),
        expires_on=expires,
    )


def _find_by_idempotency(db: Session, key: str) -> Certificate | None:
    marker = f"idk:{key}"
    return db.scalar(
        select(Certificate)
        .where(Certificate.note.contains(marker), Certificate.is_deleted.is_(False))
        .order_by(Certificate.id.desc())
    )


def _resolve_expires(raw: str) -> date:
    """Срок из запроса или год с сегодня (МСК)."""
    from datetime import datetime

    parsed = _parse_expires(raw)
    if parsed is not None:
        return parsed
    today_msk = datetime.now(MSK).date()
    return today_msk + timedelta(days=365)


def _parse_expires(raw: str) -> date | None:
    try:
        return parse_date(raw)
    except Exception:
        return None


def _holder_full_name(
    first_name: str,
    last_name: str,
    phone: str,
    order_id: str,
) -> str:
    first = " ".join(first_name.split()).strip()
    last = " ".join(last_name.split()).strip()
    if first or last:
        return f"{last} {first}".strip()[:120]
    if _looks_like_phone(phone):
        return f"Покупатель {phone[-4:]}"[:120]
    if order_id:
        return f"Покупатель {order_id[-6:]}"[:120]
    return "Покупатель Джани"


def _looks_like_phone(value: str) -> bool:
    digits = "".join(ch for ch in value if ch.isdigit())
    return len(digits) >= 10

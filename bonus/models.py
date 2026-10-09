"""Таблицы сертификатов, людей и журнала списаний."""

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Общая база моделей."""


class Role(str, enum.Enum):
    """Кто входит в систему."""

    employee = "employee"
    manager = "manager"
    hr = "hr"


class CertificateStatus(str, enum.Enum):
    """Можно ли списывать бонусы."""

    active = "active"
    blocked = "blocked"


class TxType(str, enum.Enum):
    """Вид записи в журнале."""

    issue = "issue"
    debit = "debit"
    credit = "credit"
    block = "block"
    unblock = "unblock"
    migrate = "migrate"
    expire = "expire"


class AccrualPeriod(str, enum.Enum):
    """Как часто сегмент предполагает начисление бонусов. none — без автоначисления."""

    none = "none"
    daily = "daily"
    monthly = "monthly"
    yearly = "yearly"


class User(Base):
    """Сотрудник, управляющий или HR."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=16), index=True)
    restaurant: Mapped[str] = mapped_column(String(200), default="")
    department: Mapped[str] = mapped_column(String(120), default="")
    job_title: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    must_change_password: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: _utcnow())

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="holder",
        foreign_keys="Certificate.holder_id",
    )


class Segment(Base):
    """Тип сертификата: ланч, бартер, день рождения и свои сегменты."""

    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    accrual_period: Mapped[AccrualPeriod] = mapped_column(
        Enum(AccrualPeriod, native_enum=False, length=16),
        default=AccrualPeriod.none,
    )
    requires_expiry: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)


class CardTemplate(Base):
    """Картинка баннера, которую HR выбирает при выпуске."""

    __tablename__ = "card_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    image_path: Mapped[str] = mapped_column(String(200))
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: _utcnow())


class Certificate(Base):
    """Именной сертификат с остатком бонусов."""

    __tablename__ = "certificates"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_certificate_balance_nonnegative"),
        CheckConstraint("nominal >= 0", name="ck_certificate_nominal_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_number: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    short_code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    holder_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("segments.id"), nullable=True, index=True)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("card_templates.id"), nullable=True)
    color: Mapped[str] = mapped_column(String(7), default="#00A8A8")
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    nominal: Mapped[int] = mapped_column(Integer)
    balance: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[CertificateStatus] = mapped_column(
        Enum(CertificateStatus, native_enum=False, length=16),
        default=CertificateStatus.active,
        index=True,
    )
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: _utcnow())

    holder: Mapped[User] = relationship(back_populates="certificates", foreign_keys=[holder_id])
    segment: Mapped[Segment | None] = relationship()
    template: Mapped[CardTemplate | None] = relationship()


class Transaction(Base):
    """Одна операция по сертификату. Суммы хранятся в копейках."""

    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("reversal_of_id", name="uq_transaction_reversal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    certificate_id: Mapped[int] = mapped_column(ForeignKey("certificates.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    tx_type: Mapped[TxType] = mapped_column(Enum(TxType, native_enum=False, length=16), index=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    balance_after: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(String(300), default="")
    restaurant: Mapped[str] = mapped_column(String(200), default="")
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _utcnow(),
        index=True,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

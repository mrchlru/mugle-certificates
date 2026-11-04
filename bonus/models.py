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
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bonus.ledger import LedgerSheet


class Base(DeclarativeBase):
    """Общая база моделей."""


class Role(str, enum.Enum):
    """Кто входит в систему."""

    employee = "employee"
    manager = "manager"
    hr = "hr"
    hrd = "hrd"
    admin = "admin"


class AuditAction(str, enum.Enum):
    """Действие сотрудника в админке для журнала контроля."""

    staff_create = "staff_create"
    staff_update = "staff_update"
    staff_active = "staff_active"
    certificate_issue = "certificate_issue"
    certificate_credit = "certificate_credit"
    certificate_debit = "certificate_debit"
    certificate_block = "certificate_block"
    certificate_unblock = "certificate_unblock"
    certificate_expiry = "certificate_expiry"
    debit_undo = "debit_undo"
    import_rows = "import_rows"
    certificate_delete = "certificate_delete"
    template_delete = "template_delete"
    segment_delete = "segment_delete"
    link_export = "link_export"
    link_delivery_mark = "link_delivery_mark"
    login_unblock = "login_unblock"
    sheets_dedupe = "sheets_dedupe"


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
    burn = "burn"


class AccrualPeriod(str, enum.Enum):
    """Как часто сегмент предполагает начисление бонусов. none — без автоначисления."""

    none = "none"
    daily = "daily"
    weekly = "weekly"
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
    # Связь с приложением «Спасибо» (MugleHRbotTopManagment): users.id из их PostgreSQL.
    spasibo_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    # Какую employee-карту показывать в «Спасибо», если у держателя несколько.
    spasibo_certificate_id: Mapped[int | None] = mapped_column(
        ForeignKey("certificates.id"),
        nullable=True,
        index=True,
    )
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
    single_use: Mapped[bool] = mapped_column(default=False)
    # Распаковка: "" | "birthday" | "gift" (бартер и прочие подарки).
    unwrap_kind: Mapped[str] = mapped_column(String(16), default="")
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    # Маска дней ISO: 1=Пн … 7=Вс, например "12345" = будни.
    use_days: Mapped[str] = mapped_column(String(16), default="1234567")
    accrual_days: Mapped[str] = mapped_column(String(16), default="12345")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    # Мягкое удаление: скрыт из каталога новых карт, старые карты сохраняют FK.
    is_deleted: Mapped[bool] = mapped_column(default=False)


class CardTemplate(Base):
    """Баннер, логотип, цвет и сегмент(ы) для выпуска карточки."""

    __tablename__ = "card_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    image_path: Mapped[str] = mapped_column(String(200))
    logo_path: Mapped[str] = mapped_column(String(200), default="")
    color: Mapped[str] = mapped_column(String(7), default="#00A8A8")
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("segments.id"), nullable=True, index=True)
    # Второй сегмент для комбинированной карты (ланч + ежемесячные и т.п.).
    segment2_id: Mapped[int | None] = mapped_column(
        ForeignKey("segments.id"), nullable=True, index=True
    )
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    # Мягкое удаление: не для новых карт; выпущенные сохраняют визуал по template_id.
    is_deleted: Mapped[bool] = mapped_column(default=False)
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
    single_use: Mapped[bool] = mapped_column(default=False)
    use_days: Mapped[str] = mapped_column(String(16), default="1234567")
    accrual_days: Mapped[str] = mapped_column(String(16), default="12345")
    nominal: Mapped[int] = mapped_column(Integer)
    balance: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(500), default="")
    ledger_sheet: Mapped[str] = mapped_column(
        String(32),
        default=LedgerSheet.employee.value,
        index=True,
    )
    company: Mapped[str] = mapped_column(String(200), default="")
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[CertificateStatus] = mapped_column(
        Enum(CertificateStatus, native_enum=False, length=16),
        default=CertificateStatus.active,
        index=True,
    )
    # Мягкое удаление: ссылка открывает анимацию «карта недоступна», из списков HR скрыта.
    is_deleted: Mapped[bool] = mapped_column(default=False, index=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # True при выпуске из сегмента с распаковкой; False после первого открытия.
    unwrap_pending: Mapped[bool] = mapped_column(default=False)
    gift_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Тестовая карта: кнопки кошелька видны даже при выключенной глобальной настройке.
    show_wallet_buttons: Mapped[bool] = mapped_column(default=False)
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: _utcnow())

    holder: Mapped[User] = relationship(back_populates="certificates", foreign_keys=[holder_id])
    segment: Mapped[Segment | None] = relationship()
    template: Mapped[CardTemplate | None] = relationship()
    parts: Mapped[list["CertificatePart"]] = relationship(
        back_populates="certificate",
        cascade="all, delete-orphan",
        order_by="CertificatePart.sort_order",
    )


class CertificatePart(Base):
    """Часть комбинированной (или обычной) карты: свой сегмент, номинал и остаток.

    Правила начисления/дней копируются из сегмента при выпуске и дальше живут
    независимо от правок каталога сегментов.
    """

    __tablename__ = "certificate_parts"
    __table_args__ = (
        UniqueConstraint("certificate_id", "segment_id", name="uq_cert_part_segment"),
        CheckConstraint("balance >= 0", name="ck_part_balance_nonnegative"),
        CheckConstraint("nominal >= 0", name="ck_part_nominal_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    certificate_id: Mapped[int] = mapped_column(ForeignKey("certificates.id"), index=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("segments.id"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=1)
    segment_name: Mapped[str] = mapped_column(String(120), default="")
    accrual_period: Mapped[AccrualPeriod] = mapped_column(
        Enum(AccrualPeriod, native_enum=False, length=16),
        default=AccrualPeriod.none,
    )
    use_days: Mapped[str] = mapped_column(String(16), default="1234567")
    accrual_days: Mapped[str] = mapped_column(String(16), default="12345")
    nominal: Mapped[int] = mapped_column(Integer, default=0)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _utcnow(),
    )

    certificate: Mapped[Certificate] = relationship(back_populates="parts")
    segment: Mapped[Segment | None] = relationship()


class AppSettings(Base):
    """Единственная строка настроек приложения (id=1)."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_buttons_enabled: Mapped[bool] = mapped_column(default=False)


class SheetsSyncJob(Base):
    """Очередь дозаписи сертификата в Google Sheets (чтобы не упираться в квоту)."""

    __tablename__ = "sheets_sync_jobs"
    __table_args__ = (UniqueConstraint("certificate_id", name="uq_sheets_job_cert"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    certificate_id: Mapped[int] = mapped_column(ForeignKey("certificates.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _utcnow(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _utcnow(),
    )


class Transaction(Base):
    """Одна операция по сертификату. Суммы хранятся в копейках."""

    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("reversal_of_id", name="uq_transaction_reversal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    certificate_id: Mapped[int] = mapped_column(ForeignKey("certificates.id"), index=True)
    part_id: Mapped[int | None] = mapped_column(
        ForeignKey("certificate_parts.id"), nullable=True, index=True
    )
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    tx_type: Mapped[TxType] = mapped_column(Enum(TxType, native_enum=False, length=16), index=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    balance_after: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(String(300), default="")
    restaurant: Mapped[str] = mapped_column(String(200), default="")
    order_ref: Mapped[str] = mapped_column(String(80), default="")
    cheque_detail: Mapped[str] = mapped_column(Text, default="")
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _utcnow(),
        index=True,
    )


class LinkDelivery(Base):
    """Отметка, что ссылка на карту уже отправлена получателю (рассылка)."""

    __tablename__ = "link_deliveries"
    __table_args__ = (UniqueConstraint("certificate_id", name="uq_link_delivery_cert"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    certificate_id: Mapped[int] = mapped_column(ForeignKey("certificates.id"), index=True)
    group_label: Mapped[str] = mapped_column(String(200), default="")
    marked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    marked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _utcnow(),
        index=True,
    )


class AuditLog(Base):
    """Запись о действии сотрудника в админке для контроля admin и HRD."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(String(400), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _utcnow(),
        index=True,
    )


class LoginAttempt(Base):
    """Счётчик неудачных входов по IP (блок после лимита попыток)."""

    __tablename__ = "login_attempts"

    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_fail_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

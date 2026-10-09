"""Снимки данных для страниц. В шаблон не попадают живые объекты базы."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bonus.models import Certificate, CertificateStatus, Role, Transaction, TxType, User
from bonus.qr import qr_svg
from bonus.values import MSK, as_utc, today_msk

UNDO_WINDOW = timedelta(minutes=15)


@dataclass(frozen=True)
class TxView:
    """Строка истории на карточке или в журнале."""

    id: int
    tx_type: str
    amount: int
    balance_after: int
    comment: str
    created_at: datetime
    actor_name: str
    holder_name: str
    public_number: str
    reversal_of_id: int | None
    undone: bool
    can_undo: bool


@dataclass(frozen=True)
class CardView:
    """Карточка, которую видит сотрудник или HR."""

    id: int
    public_number: str
    short_code: str
    balance: int
    nominal: int
    note: str
    expires_on: date | None
    holder_name: str
    holder_login: str
    status: CertificateStatus
    can_spend: bool
    qr_svg: str
    history: tuple[TxView, ...]


@dataclass(frozen=True)
class DashboardStats:
    """Три цифры на сводке HR."""

    active_count: int
    total_balance: int
    spent_today: int


@dataclass(frozen=True)
class StaffView:
    """Управляющий или HR в списке доступа."""

    id: int
    full_name: str
    login: str
    role: Role
    restaurant: str
    is_active: bool


def load_cards(db: Session, holder_id: int, base_url: str, actor: User) -> list[CardView]:
    """Все сертификаты одного сотрудника."""
    certificates = db.scalars(
        select(Certificate).where(Certificate.holder_id == holder_id).order_by(Certificate.id.desc())
    ).all()
    return [load_card(db, certificate, base_url, actor) for certificate in certificates]


def load_card(db: Session, certificate: Certificate, base_url: str, actor: User | None) -> CardView:
    """Одна карточка вместе с историей."""
    holder = db.get(User, certificate.holder_id)
    holder_name = holder.full_name if holder is not None else ""
    login = holder.login if holder is not None else ""
    history = tuple(_history(db, certificate.id, actor, holder_name, certificate.public_number))
    return CardView(
        id=certificate.id,
        public_number=certificate.public_number,
        short_code=certificate.short_code,
        balance=certificate.balance,
        nominal=certificate.nominal,
        note=certificate.note,
        expires_on=certificate.expires_on,
        holder_name=holder_name,
        holder_login=login,
        status=certificate.status,
        can_spend=_can_spend(certificate),
        qr_svg=qr_svg(f"{base_url}/m/{certificate.token}"),
        history=history,
    )


def recent_debits(db: Session, actor: User, limit: int = 8) -> list[TxView]:
    """Последние списания этого управляющего."""
    rows = db.execute(
        select(Transaction, Certificate.public_number, User.full_name)
        .join(Certificate, Transaction.certificate_id == Certificate.id)
        .join(User, Certificate.holder_id == User.id)
        .where(Transaction.actor_id == actor.id, Transaction.tx_type == TxType.debit)
        .order_by(Transaction.id.desc())
        .limit(limit)
    ).all()
    undone = _undone_ids(db, [row[0].id for row in rows])
    return [
        _tx_view(tx, actor.full_name, holder_name, public_number, tx.id in undone, actor)
        for tx, public_number, holder_name in rows
    ]


def search_certificates(db: Session, query: str) -> list[CardView]:
    """Список для HR. Поиск по имени, логину и номеру без учёта регистра."""
    rows = db.execute(
        select(Certificate, User)
        .join(User, Certificate.holder_id == User.id)
        .order_by(Certificate.id.desc())
    ).all()
    needle = query.strip().casefold()
    found: list[CardView] = []
    for certificate, holder in rows:
        haystack = " ".join(
            (holder.full_name, holder.login, certificate.public_number, certificate.short_code, certificate.note)
        ).casefold()
        if needle and needle not in haystack:
            continue
        found.append(
            CardView(
                id=certificate.id,
                public_number=certificate.public_number,
                short_code=certificate.short_code,
                balance=certificate.balance,
                nominal=certificate.nominal,
                note=certificate.note,
                expires_on=certificate.expires_on,
                holder_name=holder.full_name,
                holder_login=holder.login,
                status=certificate.status,
                can_spend=_can_spend(certificate),
                qr_svg="",
                history=(),
            )
        )
        if len(found) >= 300:
            break
    return found


def dashboard_stats(db: Session) -> DashboardStats:
    """Сколько живых сертификатов, какой общий остаток и сколько списали сегодня."""
    active_count = int(
        db.scalar(
            select(func.count()).select_from(Certificate).where(Certificate.status == CertificateStatus.active)
        )
        or 0
    )
    total_balance = int(
        db.scalar(
            select(func.coalesce(func.sum(Certificate.balance), 0)).where(
                Certificate.status == CertificateStatus.active
            )
        )
        or 0
    )
    start = _moscow_day_start()
    reversed_ids = select(Transaction.reversal_of_id).where(Transaction.reversal_of_id.is_not(None))
    spent_today = int(
        db.scalar(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.tx_type == TxType.debit,
                Transaction.created_at >= start,
                Transaction.id.not_in(reversed_ids),
            )
        )
        or 0
    )
    return DashboardStats(active_count=active_count, total_balance=total_balance, spent_today=spent_today)


def ledger(db: Session, actor: User, limit: int = 200) -> list[TxView]:
    """Журнал операций, новые сверху."""
    rows = db.execute(
        select(Transaction, Certificate.public_number, User.full_name)
        .join(Certificate, Transaction.certificate_id == Certificate.id)
        .join(User, Certificate.holder_id == User.id)
        .order_by(Transaction.id.desc())
        .limit(limit)
    ).all()
    actor_ids = {row[0].actor_id for row in rows if row[0].actor_id is not None}
    names = _user_names(db, actor_ids)
    undone = _undone_ids(db, [row[0].id for row in rows])
    return [
        _tx_view(
            tx,
            names.get(tx.actor_id or 0, ""),
            holder_name,
            public_number,
            tx.id in undone,
            actor,
        )
        for tx, public_number, holder_name in rows
    ]


def list_staff(db: Session) -> list[StaffView]:
    """Управляющие и HR."""
    users = db.scalars(
        select(User).where(User.role != Role.employee).order_by(User.role, User.full_name)
    ).all()
    return [
        StaffView(
            id=user.id,
            full_name=user.full_name,
            login=user.login,
            role=user.role,
            restaurant=user.restaurant,
            is_active=user.is_active,
        )
        for user in users
    ]


def _history(
    db: Session,
    certificate_id: int,
    actor: User | None,
    holder_name: str,
    public_number: str,
) -> list[TxView]:
    rows = db.execute(
        select(Transaction, User.full_name)
        .outerjoin(User, Transaction.actor_id == User.id)
        .where(Transaction.certificate_id == certificate_id)
        .order_by(Transaction.id.desc())
    ).all()
    undone = _undone_ids(db, [row[0].id for row in rows])
    return [
        _tx_view(tx, actor_name or "", holder_name, public_number, tx.id in undone, actor)
        for tx, actor_name in rows
    ]


def _tx_view(
    tx: Transaction,
    actor_name: str,
    holder_name: str,
    public_number: str,
    undone: bool,
    viewer: User | None,
) -> TxView:
    return TxView(
        id=tx.id,
        tx_type=tx.tx_type.value,
        amount=tx.amount,
        balance_after=tx.balance_after,
        comment=tx.comment,
        created_at=tx.created_at,
        actor_name=actor_name or "",
        holder_name=holder_name,
        public_number=public_number,
        reversal_of_id=tx.reversal_of_id,
        undone=undone,
        can_undo=_can_undo(tx, viewer, undone),
    )


def _can_spend(certificate: Certificate) -> bool:
    if certificate.status != CertificateStatus.active or certificate.balance <= 0:
        return False
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return False
    return True


def _can_undo(tx: Transaction, viewer: User | None, undone: bool) -> bool:
    if viewer is None or undone or tx.tx_type != TxType.debit:
        return False
    if viewer.role == Role.hr:
        return True
    if viewer.role != Role.manager or tx.actor_id != viewer.id:
        return False
    age = datetime.now(timezone.utc) - as_utc(tx.created_at)
    return age <= UNDO_WINDOW


def _undone_ids(db: Session, tx_ids: list[int]) -> set[int]:
    if not tx_ids:
        return set()
    found = db.scalars(select(Transaction.reversal_of_id).where(Transaction.reversal_of_id.in_(tx_ids))).all()
    return {item for item in found if item is not None}


def _user_names(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = db.execute(select(User.id, User.full_name).where(User.id.in_(user_ids))).all()
    return {user_id: name for user_id, name in rows}


def _moscow_day_start() -> datetime:
    now = datetime.now(MSK)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)

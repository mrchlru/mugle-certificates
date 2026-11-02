"""Снимки данных для страниц. В шаблон не попадают живые объекты базы."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import ColumnElement, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from bonus.catalog import media_url
from bonus.models import (
    CardTemplate,
    Certificate,
    CertificatePart,
    CertificateStatus,
    Role,
    Segment,
    Transaction,
    TxType,
    User,
)
from bonus.barcode_draw import barcode_svg
from bonus.roles import role_label
from bonus.services import apply_due_expiries, apply_expiry_writeoff
from bonus.values import MSK, as_utc, format_short_code, today_msk

UNDO_WINDOW = timedelta(minutes=15)
HISTORY_PAGE_SIZE = 10


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
    restaurant: str
    order_ref: str
    cheque_detail: str
    holder_name: str
    public_number: str
    reversal_of_id: int | None
    undone: bool
    can_undo: bool


@dataclass(frozen=True)
class PartView:
    """Одна часть комбинированной карты для админки."""

    id: int
    segment_id: int
    segment_name: str
    accrual_period: str
    nominal: int
    balance: int
    use_days: str
    accrual_days: str
    available_now: bool


@dataclass(frozen=True)
class CardView:
    """Карточка, которую видит сотрудник или HR."""

    id: int
    public_number: str
    short_code: str
    token: str
    balance: int
    nominal: int
    note: str
    expires_on: date | None
    holder_name: str
    holder_login: str
    department: str
    job_title: str
    phone: str
    company: str
    birth_date: date | None
    ledger_sheet: str
    status: CertificateStatus
    can_spend: bool
    color: str
    discount_percent: int
    single_use: bool
    segment_name: str
    template_url: str
    logo_url: str
    logo_custom: bool
    barcode_value: str
    barcode_svg: str
    use_days: str
    use_days_label: str
    use_days_active: tuple[str, ...]
    birthday_unwrap: bool
    gift_opened: bool
    unwrap_pending: bool
    unwrap_kind: str
    first_name: str
    history: tuple[TxView, ...]
    history_has_more: bool = False
    history_next_before: int | None = None
    parts: tuple[PartView, ...] = ()
    is_combo: bool = False


@dataclass(frozen=True)
class HistoryPage:
    """Порция истории операций по карте."""

    items: tuple[TxView, ...]
    has_more: bool
    next_before: int | None


@dataclass(frozen=True)
class DashboardStats:
    """Три цифры на сводке HR."""

    active_count: int
    total_balance: int
    spent_today: int


@dataclass(frozen=True)
class CertificateSearchPage:
    """Страница сводки с ленивой подгрузкой (полный CardView — эталон/legacy)."""

    cards: tuple[CardView, ...]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        """Есть ли ещё карты после текущей страницы."""
        return self.offset + len(self.cards) < self.total

    @property
    def next_offset(self) -> int:
        """Смещение для следующего запроса."""
        return self.offset + len(self.cards)


@dataclass(frozen=True)
class CardRow:
    """Лёгкая строка сводки HR без штрихкода, частей и истории."""

    id: int
    public_number: str
    color: str
    token: str
    holder_name: str
    department: str
    job_title: str
    phone: str
    segment_name: str
    balance: int
    expires_on: date | None
    status: CertificateStatus
    single_use: bool


@dataclass(frozen=True)
class CertificateRowsPage:
    """Страница сводки на лёгких строках (тот же контракт, что CertificateSearchPage)."""

    cards: tuple[CardRow, ...]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        """Есть ли ещё карты после текущей страницы."""
        return self.offset + len(self.cards) < self.total

    @property
    def next_offset(self) -> int:
        """Смещение для следующего запроса."""
        return self.offset + len(self.cards)


@dataclass(frozen=True)
class DashBucket:
    """Именованная группа для полосок дашборда."""

    name: str
    count: int
    amount: int
    share: float


@dataclass(frozen=True)
class DashDay:
    """День в мини-графике списаний и начислений."""

    day: date
    label: str
    debit: int
    credit: int
    debit_share: float
    credit_share: float


@dataclass(frozen=True)
class DashCardRow:
    """Короткая строка карты для списков дашборда."""

    id: int
    public_number: str
    holder_name: str
    segment_name: str
    balance: int
    color: str
    expires_on: date | None


@dataclass(frozen=True)
class AdminDashboard:
    """Полная сводка для admin-дашборда."""

    active_count: int
    blocked_count: int
    zero_balance_count: int
    expiring_soon_count: int
    total_balance: int
    spent_today: int
    spent_month: int
    credited_today: int
    credited_month: int
    burned_month: int
    issued_month: int
    by_segment: tuple[DashBucket, ...]
    by_ledger: tuple[DashBucket, ...]
    by_restaurant: tuple[DashBucket, ...]
    days: tuple[DashDay, ...]
    recent_debits: tuple[TxView, ...]
    expiring: tuple[DashCardRow, ...]
    top_balance: tuple[DashCardRow, ...]


@dataclass(frozen=True)
class StaffView:
    """Управляющий или HR в списке доступа."""

    id: int
    full_name: str
    first_name: str
    last_name: str
    login: str
    role: Role
    restaurant: str
    phone: str
    is_active: bool


def load_cards(db: Session, holder_id: int, base_url: str, actor: User) -> list[CardView]:
    """Все сертификаты одного сотрудника (без мягко удалённых)."""
    certificates = db.scalars(
        select(Certificate)
        .where(
            Certificate.holder_id == holder_id,
            Certificate.is_deleted.is_(False),
        )
        .options(
            selectinload(Certificate.parts),
            selectinload(Certificate.template),
            selectinload(Certificate.segment),
        )
        .order_by(Certificate.id.desc())
    ).all()
    return [load_card(db, certificate, base_url, actor) for certificate in certificates]


def load_card(db: Session, certificate: Certificate, base_url: str, actor: User | None) -> CardView:
    """Одна карточка вместе с первой порцией истории."""
    if apply_expiry_writeoff(db, certificate):
        db.commit()
        db.refresh(certificate)
    holder = db.get(User, certificate.holder_id)
    page = _history(
        db,
        certificate.id,
        actor,
        holder.full_name if holder is not None else "",
        certificate.public_number,
    )
    return _card_view(
        db,
        certificate,
        holder,
        base_url,
        page.items,
        history_has_more=page.has_more,
        history_next_before=page.next_before,
    )


def load_history_page(
    db: Session,
    certificate: Certificate,
    actor: User | None,
    *,
    limit: int = HISTORY_PAGE_SIZE,
    before_id: int | None = None,
) -> HistoryPage:
    """Следующая порция истории для ленивой подгрузки."""
    holder = db.get(User, certificate.holder_id)
    return _history(
        db,
        certificate.id,
        actor,
        holder.full_name if holder is not None else "",
        certificate.public_number,
        limit=limit,
        before_id=before_id,
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
        _tx_view(
            tx,
            actor.full_name,
            holder_name,
            public_number,
            tx.id in undone,
            actor,
            restaurant=tx.restaurant or actor.restaurant,
        )
        for tx, public_number, holder_name in rows
    ]


_SORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("name", "По алфавиту"),
    ("balance", "По балансу"),
    ("expires", "По сроку"),
)

_STATUS_CHOICES: tuple[tuple[str, str], ...] = (
    ("all", "Все статусы"),
    ("active", "Активные"),
    ("expired", "Истёкшие"),
    ("blocked", "Заблокированные"),
)


def sort_choices() -> tuple[tuple[str, str], ...]:
    """Варианты сортировки сводки для селекта."""
    return _SORT_CHOICES


def status_choices() -> tuple[tuple[str, str], ...]:
    """Варианты фильтра статуса на сводке."""
    return _STATUS_CHOICES


def normalize_sort(raw: str) -> str:
    """Допустимый ключ сортировки; иначе алфавит."""
    cleaned = (raw or "").strip().casefold()
    allowed = {key for key, _label in _SORT_CHOICES}
    return cleaned if cleaned in allowed else "name"


def normalize_status_filter(raw: str) -> str:
    """Допустимый фильтр статуса; иначе все."""
    cleaned = (raw or "").strip().casefold()
    allowed = {key for key, _label in _STATUS_CHOICES}
    return cleaned if cleaned in allowed else "all"


SUMMARY_PAGE_SIZE = 30


def search_certificate_rows(
    db: Session,
    query: str,
    segment_id: int | None = None,
    *,
    sort: str = "name",
    status: str = "all",
    offset: int = 0,
    limit: int = SUMMARY_PAGE_SIZE,
) -> CertificateRowsPage:
    """Быстрая сводка HR: фильтры и пагинация в SQL, без CardView на каждую карту."""
    apply_due_expiries(db)
    status_key = normalize_status_filter(status)
    sort_key = normalize_sort(sort)
    today = today_msk()
    start = max(0, int(offset))
    page_size = max(1, min(int(limit), 100))

    filters = _summary_filters(segment_id, status_key, today)
    search_clause = _summary_search_clause(db, query)
    if search_clause is not None:
        filters.append(search_clause)

    balance_expr = _summary_balance_expr().label("row_balance")
    segment_label = _summary_segment_label().label("segment_label")
    order_by = _summary_order_by(db, sort_key, balance_expr)

    total = int(
        db.scalar(
            select(func.count())
            .select_from(Certificate)
            .join(User, Certificate.holder_id == User.id)
            .outerjoin(Segment, Certificate.segment_id == Segment.id)
            .outerjoin(CardTemplate, Certificate.template_id == CardTemplate.id)
            .where(*filters)
        )
        or 0
    )
    rows = db.execute(
        select(
            Certificate.id,
            Certificate.public_number,
            Certificate.color,
            Certificate.token,
            User.full_name,
            User.department,
            User.job_title,
            User.phone,
            segment_label,
            balance_expr,
            Certificate.expires_on,
            Certificate.status,
            Certificate.single_use,
        )
        .join(User, Certificate.holder_id == User.id)
        .outerjoin(Segment, Certificate.segment_id == Segment.id)
        .outerjoin(CardTemplate, Certificate.template_id == CardTemplate.id)
        .where(*filters)
        .order_by(*order_by)
        .offset(start)
        .limit(page_size)
    ).all()
    cards = tuple(
        CardRow(
            id=int(row[0]),
            public_number=str(row[1] or ""),
            color=str(row[2] or "#00A8A8"),
            token=str(row[3] or ""),
            holder_name=str(row[4] or ""),
            department=str(row[5] or ""),
            job_title=str(row[6] or ""),
            phone=str(row[7] or ""),
            segment_name=str(row[8] or ""),
            balance=int(row[9] or 0),
            expires_on=row[10],
            status=row[11],
            single_use=bool(row[12]),
        )
        for row in rows
    )
    return CertificateRowsPage(
        cards=cards,
        total=total,
        offset=start,
        limit=page_size,
    )


def search_certificates(
    db: Session,
    query: str,
    segment_id: int | None = None,
    *,
    sort: str = "name",
    status: str = "all",
    offset: int = 0,
    limit: int = SUMMARY_PAGE_SIZE,
) -> CertificateRowsPage:
    """Список для HR: быстрые строки сводки (совместимое имя для старых импортов)."""
    return search_certificate_rows(
        db,
        query,
        segment_id,
        sort=sort,
        status=status,
        offset=offset,
        limit=limit,
    )


def _ci(db: Session, column: ColumnElement) -> ColumnElement:
    """Сравнение без регистра: py_casefold в SQLite, lower() иначе."""
    dialect = ""
    bind = db.get_bind()
    if bind is not None:
        dialect = (bind.dialect.name or "").casefold()
    if dialect == "sqlite":
        return func.py_casefold(column)
    return func.lower(column)


def _summary_filters(
    segment_id: int | None,
    status_key: str,
    today: date,
) -> list:
    """Общие WHERE для списка и count сводки."""
    filters: list = [Certificate.is_deleted.is_(False)]
    if segment_id is not None and segment_id > 0:
        filters.append(Certificate.segment_id == segment_id)
    if status_key == "blocked":
        filters.append(Certificate.status == CertificateStatus.blocked)
    elif status_key == "expired":
        filters.append(Certificate.expires_on.is_not(None))
        filters.append(Certificate.expires_on < today)
    elif status_key == "active":
        filters.append(Certificate.status == CertificateStatus.active)
        filters.append(
            or_(Certificate.expires_on.is_(None), Certificate.expires_on >= today)
        )
    return filters


def _summary_search_clause(db: Session, query: str):
    """LIKE по тем же полям, что искал Python casefold; пустой запрос — None."""
    needle = " ".join(query.split()).casefold()
    if not needle:
        return None
    escaped = (
        needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    pattern = f"%{escaped}%"
    fields = (
        User.full_name,
        User.department,
        User.job_title,
        User.phone,
        Certificate.public_number,
        Certificate.short_code,
        Certificate.note,
        Segment.name,
    )
    return or_(
        *(
            _ci(db, func.coalesce(column, "")).like(pattern, escape="\\")
            for column in fields
        )
    )


def _summary_balance_expr():
    """Баланс строки = сумма частей, иначе Certificate.balance."""
    parts_exist = exists(
        select(literal(1)).where(CertificatePart.certificate_id == Certificate.id)
    )
    parts_sum = (
        select(func.coalesce(func.sum(CertificatePart.balance), 0))
        .where(CertificatePart.certificate_id == Certificate.id)
        .correlate(Certificate)
        .scalar_subquery()
    )
    return case((parts_exist, parts_sum), else_=Certificate.balance)


def _summary_segment_label():
    """Подпись: имя шаблона, иначе сегмент (как в _card_view)."""
    return func.coalesce(
        func.nullif(func.trim(CardTemplate.name), ""),
        Segment.name,
        literal(""),
    )


def _summary_order_by(db: Session, sort_key: str, balance_expr) -> list:
    """ORDER BY со стабильным хвостом по Certificate.id."""
    name_ci = _ci(db, User.full_name)
    if sort_key == "balance":
        return [balance_expr.desc(), name_ci, Certificate.public_number, Certificate.id]
    if sort_key == "expires":
        return [
            Certificate.expires_on.is_(None),
            Certificate.expires_on.asc(),
            name_ci,
            Certificate.public_number,
            Certificate.id,
        ]
    return [name_ci, Certificate.public_number, Certificate.id]


def dashboard_stats(db: Session, segment_id: int | None = None) -> DashboardStats:
    """Активные, сумма на балансах и списания за сегодня — с учётом сегмента."""
    apply_due_expiries(db)
    active = Certificate.status == CertificateStatus.active
    cert_filters = [active]
    if segment_id is not None and segment_id > 0:
        cert_filters.append(Certificate.segment_id == segment_id)
    active_count = int(
        db.scalar(select(func.count()).select_from(Certificate).where(*cert_filters)) or 0
    )
    total_balance = int(
        db.scalar(
            select(func.coalesce(func.sum(Certificate.balance), 0)).where(*cert_filters)
        )
        or 0
    )
    start = _moscow_day_start()
    reversed_ids = select(Transaction.reversal_of_id).where(Transaction.reversal_of_id.is_not(None))
    spent_filters = [
        Transaction.tx_type == TxType.debit,
        Transaction.created_at >= start,
        Transaction.id.not_in(reversed_ids),
    ]
    spent_query = select(func.coalesce(func.sum(Transaction.amount), 0))
    if segment_id is not None and segment_id > 0:
        spent_query = spent_query.join(
            Certificate, Transaction.certificate_id == Certificate.id
        ).where(*spent_filters, Certificate.segment_id == segment_id)
    else:
        spent_query = spent_query.where(*spent_filters)
    spent_today = int(db.scalar(spent_query) or 0)
    return DashboardStats(active_count=active_count, total_balance=total_balance, spent_today=spent_today)


def _sort_cards(cards: list[CardView], sort: str) -> list[CardView]:
    """Сортирует карточки: алфавит, баланс убыв., срок (без даты — внизу)."""
    if sort == "balance":
        return sorted(
            cards,
            key=lambda card: (-card.balance, card.holder_name.casefold(), card.public_number),
        )
    if sort == "expires":
        return sorted(
            cards,
            key=lambda card: (
                card.expires_on is None,
                card.expires_on or date.max,
                card.holder_name.casefold(),
                card.public_number,
            ),
        )
    return sorted(
        cards,
        key=lambda card: (card.holder_name.casefold(), card.public_number),
    )


def admin_dashboard(db: Session) -> AdminDashboard:
    """Собирает KPI, разрезы и списки для дашборда администратора."""
    apply_due_expiries(db)
    today = today_msk()
    day_start = _moscow_day_start()
    month_start = _moscow_month_start()
    chart_start = _moscow_days_ago(13)
    soon = today + timedelta(days=14)
    reversed_ids = select(Transaction.reversal_of_id).where(Transaction.reversal_of_id.is_not(None))
    active = Certificate.status == CertificateStatus.active

    active_count = _count_certs(db, active)
    blocked_count = _count_certs(db, Certificate.status == CertificateStatus.blocked)
    zero_balance_count = _count_certs(db, active, Certificate.balance <= 0)
    expiring_soon_count = _count_certs(
        db,
        active,
        Certificate.expires_on.is_not(None),
        Certificate.expires_on >= today,
        Certificate.expires_on <= soon,
        Certificate.balance > 0,
    )
    total_balance = _sum_balance(db, active)
    spent_today = _sum_tx(db, TxType.debit, day_start, reversed_ids)
    spent_month = _sum_tx(db, TxType.debit, month_start, reversed_ids)
    credited_today = _sum_tx(db, TxType.credit, day_start, None)
    credited_month = _sum_tx(db, TxType.credit, month_start, None)
    burned_month = _sum_tx(db, TxType.burn, month_start, None)
    issued_month = _count_tx(db, TxType.issue, month_start)

    by_segment = _buckets_by_segment(db)
    by_ledger = _buckets_by_ledger(db)
    by_restaurant = _buckets_by_restaurant(db, month_start, reversed_ids)
    days = _day_series(db, chart_start, reversed_ids)
    recent_debits = _recent_debits(db, limit=8)
    expiring = _expiring_cards(db, today, soon, limit=40)
    top_balance = _top_balance_cards(db, limit=8)

    return AdminDashboard(
        active_count=active_count,
        blocked_count=blocked_count,
        zero_balance_count=zero_balance_count,
        expiring_soon_count=expiring_soon_count,
        total_balance=total_balance,
        spent_today=spent_today,
        spent_month=spent_month,
        credited_today=credited_today,
        credited_month=credited_month,
        burned_month=burned_month,
        issued_month=issued_month,
        by_segment=by_segment,
        by_ledger=by_ledger,
        by_restaurant=by_restaurant,
        days=days,
        recent_debits=recent_debits,
        expiring=expiring,
        top_balance=top_balance,
    )


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
    places = _user_restaurants(db, actor_ids)
    undone = _undone_ids(db, [row[0].id for row in rows])
    return [
        _tx_view(
            tx,
            names.get(tx.actor_id or 0, ""),
            holder_name,
            public_number,
            tx.id in undone,
            actor,
            restaurant=tx.restaurant or places.get(tx.actor_id or 0, ""),
        )
        for tx, public_number, holder_name in rows
    ]


def list_staff(db: Session, query: str = "") -> list[StaffView]:
    """Админ, HRD, HR и управляющие, опционально отфильтрованные поиском."""
    users = db.scalars(
        select(User).where(User.role != Role.employee).order_by(User.role, User.full_name)
    ).all()
    needle = " ".join(query.casefold().split())
    rows: list[StaffView] = []
    for user in users:
        first_name, last_name = _staff_name_parts(user.full_name)
        view = StaffView(
            id=user.id,
            full_name=user.full_name,
            first_name=first_name,
            last_name=last_name,
            login=user.login,
            role=user.role,
            restaurant=user.restaurant,
            phone=user.phone,
            is_active=user.is_active,
        )
        if needle and needle not in _staff_search_blob(view):
            continue
        rows.append(view)
    return rows


def _staff_name_parts(full_name: str) -> tuple[str, str]:
    """Разбирает «Фамилия Имя» в имя и фамилию для формы."""
    parts = full_name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[1:]), parts[0]


def _staff_search_blob(view: StaffView) -> str:
    """Строка для поиска по полям доступа."""
    return " ".join(
        part.casefold()
        for part in (
            view.full_name,
            view.login,
            view.phone,
            view.restaurant,
            role_label(view.role),
            view.role.value,
        )
        if part
    )


def _history(
    db: Session,
    certificate_id: int,
    actor: User | None,
    holder_name: str,
    public_number: str,
    *,
    limit: int = HISTORY_PAGE_SIZE,
    before_id: int | None = None,
) -> HistoryPage:
    """Порция операций карты: limit строк + флаг, есть ли ещё."""
    page_size = max(1, int(limit))
    query = (
        select(Transaction, User.full_name, User.restaurant)
        .outerjoin(User, Transaction.actor_id == User.id)
        .where(Transaction.certificate_id == certificate_id)
    )
    if before_id is not None:
        query = query.where(Transaction.id < int(before_id))
    rows = db.execute(query.order_by(Transaction.id.desc()).limit(page_size + 1)).all()
    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    undone = _undone_ids(db, [row[0].id for row in page_rows])
    items = tuple(
        _tx_view(
            tx,
            actor_name or "",
            holder_name,
            public_number,
            tx.id in undone,
            actor,
            restaurant=tx.restaurant or place or "",
        )
        for tx, actor_name, place in page_rows
    )
    next_before = items[-1].id if items else None
    return HistoryPage(items=items, has_more=has_more, next_before=next_before)


def _tx_view(
    tx: Transaction,
    actor_name: str,
    holder_name: str,
    public_number: str,
    undone: bool,
    viewer: User | None,
    restaurant: str | None = None,
) -> TxView:
    return TxView(
        id=tx.id,
        tx_type=tx.tx_type.value,
        amount=tx.amount,
        balance_after=tx.balance_after,
        comment=_public_comment(tx.comment),
        created_at=tx.created_at,
        actor_name=actor_name or "",
        restaurant=tx.restaurant or restaurant or "",
        order_ref=(tx.order_ref or "").strip(),
        cheque_detail=(tx.cheque_detail or "").strip(),
        holder_name=holder_name,
        public_number=public_number,
        reversal_of_id=tx.reversal_of_id,
        undone=undone,
        can_undo=_can_undo(tx, viewer, undone),
    )


def _public_comment(comment: str) -> str:
    """Убирает служебный маркер iiko:guid из текста для UI."""
    text = (comment or "").strip()
    if not text.startswith("iiko:"):
        return text
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _card_view(
    db: Session,
    certificate: Certificate,
    holder: User | None,
    base_url: str,
    history: tuple[TxView, ...],
    *,
    history_has_more: bool = False,
    history_next_before: int | None = None,
) -> CardView:
    from bonus.parts import list_parts
    from bonus.weekdays import allows_weekday

    template_path = ""
    logo_url = "/static/logo.svg"
    logo_custom = False
    template_name = ""
    template = certificate.template
    if certificate.template_id is not None and template is None:
        from bonus.models import CardTemplate as TemplateModel

        template = db.get(TemplateModel, certificate.template_id)
    if template is not None:
        from bonus.catalog import template_logo_is_custom, template_logo_url

        template_name = (template.name or "").strip()
        if template.image_path:
            template_path = media_url(template.image_path)
        logo_url = template_logo_url(template)
        logo_custom = template_logo_is_custom(template)
    # Части один раз: и для баланса, и для unwrap_kind.
    if certificate.parts:
        cert_parts = sorted(
            certificate.parts, key=lambda part: (part.sort_order, part.id)
        )
    else:
        cert_parts = list_parts(db, certificate.id)
    today = today_msk().isoweekday()
    part_views = tuple(
        PartView(
            id=part.id,
            segment_id=part.segment_id,
            segment_name=part.segment_name,
            accrual_period=part.accrual_period.value
            if hasattr(part.accrual_period, "value")
            else str(part.accrual_period),
            nominal=part.nominal,
            balance=part.balance,
            use_days=part.use_days,
            accrual_days=part.accrual_days,
            available_now=part.balance > 0 and allows_weekday(part.use_days, today),
        )
        for part in cert_parts
    )
    # Баланс на карточке — сумма частей. Трата комбо — по дням основной (первой) части.
    display_balance = (
        sum(max(0, part.balance) for part in cert_parts)
        if cert_parts
        else certificate.balance
    )
    spend_ok = True
    if cert_parts:
        primary = min(cert_parts, key=lambda part: (part.sort_order, part.id))
        spend_ok = allows_weekday(primary.use_days, today)
    # Подпись на карточке — название шаблона из «Оформление», иначе сегмент.
    if template_name:
        segment_label = template_name
    elif certificate.segment is not None:
        segment_label = certificate.segment.name or ""
    else:
        segment_label = _segment_name(db, certificate.segment_id)
    unwrap_kind = _unwrap_kind_for_certificate(db, certificate, parts=cert_parts)
    return CardView(
        id=certificate.id,
        public_number=certificate.public_number,
        short_code=certificate.short_code,
        token=certificate.token,
        balance=display_balance,
        nominal=certificate.nominal,
        note=certificate.note,
        expires_on=certificate.expires_on,
        holder_name="" if holder is None else holder.full_name,
        holder_login="" if holder is None else holder.login,
        department="" if holder is None else holder.department,
        job_title="" if holder is None else holder.job_title,
        phone="" if holder is None else holder.phone,
        company=(certificate.company or "").strip(),
        birth_date=certificate.birth_date,
        ledger_sheet=certificate.ledger_sheet or "",
        status=certificate.status,
        can_spend=_can_spend(certificate, display_balance) and spend_ok,
        color=certificate.color or "#00A8A8",
        discount_percent=certificate.discount_percent or 0,
        single_use=bool(certificate.single_use),
        segment_name=segment_label,
        template_url=template_path,
        logo_url=logo_url,
        logo_custom=logo_custom,
        barcode_value=format_short_code(certificate.short_code),
        barcode_svg=barcode_svg(certificate.short_code.replace("-", "").replace(" ", "")),
        use_days=_use_days_mask(certificate),
        use_days_label=_use_days_caption(certificate),
        use_days_active=_use_days_active(certificate),
        birthday_unwrap=unwrap_kind == "birthday",
        gift_opened=certificate.gift_opened_at is not None,
        unwrap_pending=bool(getattr(certificate, "unwrap_pending", False)),
        unwrap_kind=unwrap_kind,
        first_name=_first_name("" if holder is None else holder.full_name),
        history=history,
        history_has_more=history_has_more,
        history_next_before=history_next_before,
        parts=part_views,
        is_combo=len(part_views) > 1,
    )


def _unwrap_kind_for_certificate(
    db: Session,
    certificate: Certificate,
    *,
    parts: list | None = None,
) -> str:
    """Вид распаковки: первое непустое среди частей / сегмента карты."""
    from bonus.parts import list_parts, unwrap_kind_from_segments

    cert_parts = parts if parts is not None else list_parts(db, certificate.id)
    part_ids = [part.segment_id for part in cert_parts]
    if part_ids:
        return unwrap_kind_from_segments(db, part_ids)
    if certificate.segment_id is None:
        return ""
    return unwrap_kind_from_segments(db, [certificate.segment_id])


def preview_birthday_card(db: Session, segment: Segment) -> CardView:
    """Карточка-макет для предпросмотра распаковки сегмента."""
    from bonus.catalog import template_logo_is_custom, template_logo_url
    from bonus.models import CardTemplate

    template = db.scalar(
        select(CardTemplate)
        .where(CardTemplate.segment_id == segment.id, CardTemplate.is_active.is_(True))
        .order_by(CardTemplate.id.desc())
        .limit(1)
    )
    color = (template.color if template is not None else None) or "#B91C1C"
    template_path = ""
    logo_url = "/static/logo.svg"
    logo_custom = False
    discount = segment.discount_percent or 0
    if template is not None:
        if template.image_path:
            template_path = media_url(template.image_path)
        logo_url = template_logo_url(template)
        logo_custom = template_logo_is_custom(template)
        if template.discount_percent:
            discount = template.discount_percent
    short_code = "12345678"
    return CardView(
        id=0,
        public_number="TB-PREVIEW",
        short_code=short_code,
        token="preview",
        balance=300000,
        nominal=300000,
        note="",
        expires_on=None,
        holder_name="Марков Роман",
        holder_login="",
        department="",
        job_title="",
        phone="",
        company="",
        birth_date=None,
        ledger_sheet="gift_birthday",
        status=CertificateStatus.active,
        can_spend=False,
        color=color,
        discount_percent=discount,
        single_use=bool(segment.single_use),
        segment_name=segment.name,
        template_url=template_path,
        logo_url=logo_url,
        logo_custom=logo_custom,
        barcode_value=format_short_code(short_code),
        barcode_svg=barcode_svg(short_code),
        use_days=segment.use_days or "1234567",
        use_days_label="",
        use_days_active=tuple(),
        birthday_unwrap=_preview_unwrap_kind(segment) == "birthday",
        gift_opened=False,
        unwrap_pending=True,
        unwrap_kind=_preview_unwrap_kind(segment) or "birthday",
        first_name="Роман",
        history=tuple(),
    )


def _preview_unwrap_kind(segment: Segment) -> str:
    """Вид распаковки для предпросмотра сегмента."""
    kind = (getattr(segment, "unwrap_kind", None) or "").strip().lower()
    if kind in {"birthday", "gift"}:
        return kind
    if getattr(segment, "birthday_unwrap", False):
        return "birthday"
    return "birthday"


def _first_name(full_name: str) -> str:
    """Имя из «Фамилия Имя …»: второе слово; иначе пусто."""
    parts = [part for part in (full_name or "").split() if part]
    if len(parts) >= 2:
        return parts[1]
    return ""


def _use_days_mask(certificate: Certificate) -> str:
    from bonus.weekdays import ALL_DAYS, normalize_days

    return normalize_days(getattr(certificate, "use_days", None), default=ALL_DAYS)


def _use_days_caption(certificate: Certificate) -> str:
    from bonus.weekdays import days_label

    return days_label(_use_days_mask(certificate))


def _use_days_active(certificate: Certificate) -> tuple[str, ...]:
    from bonus.weekdays import day_set

    return tuple(sorted(day_set(_use_days_mask(certificate))))


def _segment_name(db: Session, segment_id: int | None) -> str:
    if segment_id is None:
        return ""
    segment = db.get(Segment, segment_id)
    return segment.name if segment is not None else ""


def _can_spend(certificate: Certificate, available: int | None = None) -> bool:
    """Можно ли тратить: активна, не просрочена, есть доступный сейчас остаток."""
    balance = certificate.balance if available is None else available
    if certificate.status != CertificateStatus.active or balance <= 0:
        return False
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        return False
    return True


def _can_undo(tx: Transaction, viewer: User | None, undone: bool) -> bool:
    if viewer is None or undone or tx.tx_type != TxType.debit:
        return False
    if viewer.role in {Role.admin, Role.hr, Role.hrd}:
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


def _user_restaurants(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = db.execute(select(User.id, User.restaurant).where(User.id.in_(user_ids))).all()
    return {user_id: place for user_id, place in rows}


def _moscow_day_start() -> datetime:
    now = datetime.now(MSK)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def _moscow_month_start() -> datetime:
    now = datetime.now(MSK)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def _moscow_days_ago(days: int) -> datetime:
    now = datetime.now(MSK)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def _count_certs(db: Session, *clauses: object) -> int:
    statement = (
        select(func.count())
        .select_from(Certificate)
        .where(Certificate.is_deleted.is_(False))
    )
    for clause in clauses:
        statement = statement.where(clause)
    return int(db.scalar(statement) or 0)


def _sum_balance(db: Session, *clauses: object) -> int:
    statement = select(func.coalesce(func.sum(Certificate.balance), 0)).where(
        Certificate.is_deleted.is_(False)
    )
    for clause in clauses:
        statement = statement.where(clause)
    return int(db.scalar(statement) or 0)


def _sum_tx(
    db: Session,
    tx_type: TxType,
    since: datetime,
    reversed_ids: object | None,
) -> int:
    statement = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.tx_type == tx_type,
        Transaction.created_at >= since,
    )
    if reversed_ids is not None:
        statement = statement.where(Transaction.id.not_in(reversed_ids))
    return int(db.scalar(statement) or 0)


def _count_tx(db: Session, tx_type: TxType, since: datetime) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.tx_type == tx_type, Transaction.created_at >= since)
        )
        or 0
    )


def _share(value: int, peak: int) -> float:
    if peak <= 0 or value <= 0:
        return 0.0
    return round(100.0 * value / peak, 1)


def _buckets_from_rows(rows: list[tuple[str, int, int]]) -> tuple[DashBucket, ...]:
    peak = max((amount for _, _, amount in rows), default=0)
    return tuple(
        DashBucket(name=name, count=count, amount=amount, share=_share(amount, peak))
        for name, count, amount in rows
        if count > 0 or amount > 0
    )


def _buckets_by_segment(db: Session) -> tuple[DashBucket, ...]:
    rows = db.execute(
        select(
            func.coalesce(Segment.name, "Без сегмента"),
            func.count(Certificate.id),
            func.coalesce(func.sum(Certificate.balance), 0),
        )
        .select_from(Certificate)
        .outerjoin(Segment, Certificate.segment_id == Segment.id)
        .where(Certificate.status == CertificateStatus.active)
        .group_by(Segment.name)
        .order_by(func.coalesce(func.sum(Certificate.balance), 0).desc())
    ).all()
    return _buckets_from_rows([(str(name), int(count), int(amount)) for name, count, amount in rows])


def _buckets_by_ledger(db: Session) -> tuple[DashBucket, ...]:
    from bonus.ledger import LEDGER_CHOICES

    titles = dict(LEDGER_CHOICES)
    rows = db.execute(
        select(
            Certificate.ledger_sheet,
            func.count(Certificate.id),
            func.coalesce(func.sum(Certificate.balance), 0),
        )
        .where(Certificate.status == CertificateStatus.active)
        .group_by(Certificate.ledger_sheet)
        .order_by(func.coalesce(func.sum(Certificate.balance), 0).desc())
    ).all()
    packed = [
        (titles.get(str(sheet), str(sheet) or "Прочее"), int(count), int(amount))
        for sheet, count, amount in rows
    ]
    return _buckets_from_rows(packed)


def _buckets_by_restaurant(
    db: Session,
    since: datetime,
    reversed_ids: object,
) -> tuple[DashBucket, ...]:
    """Списания по ресторану за период; пустое имя → «Без ресторана»."""
    # Группируем по сырой колонке: Postgres ругается на coalesce() с разными bind-параметрами в SELECT/GROUP BY.
    rows = db.execute(
        select(
            Transaction.restaurant,
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.amount), 0),
        )
        .where(
            Transaction.tx_type == TxType.debit,
            Transaction.created_at >= since,
            Transaction.id.not_in(reversed_ids),
        )
        .group_by(Transaction.restaurant)
        .order_by(func.coalesce(func.sum(Transaction.amount), 0).desc())
        .limit(8)
    ).all()
    packed: dict[str, list[int]] = {}
    for raw_name, count, amount in rows:
        name = (raw_name or "").strip() or "Без ресторана"
        current = packed.setdefault(name, [0, 0])
        current[0] += int(count)
        current[1] += int(amount)
    ordered = sorted(packed.items(), key=lambda item: item[1][1], reverse=True)
    return _buckets_from_rows([(name, totals[0], totals[1]) for name, totals in ordered])


def _day_series(db: Session, since: datetime, reversed_ids: object) -> tuple[DashDay, ...]:
    today = today_msk()
    days = [today - timedelta(days=offset) for offset in range(13, -1, -1)]
    debit_map = _tx_by_msk_day(db, TxType.debit, since, reversed_ids)
    credit_map = _tx_by_msk_day(db, TxType.credit, since, None)
    peak = max([*debit_map.values(), *credit_map.values(), 0])
    return tuple(
        DashDay(
            day=day,
            label=day.strftime("%d.%m"),
            debit=debit_map.get(day, 0),
            credit=credit_map.get(day, 0),
            debit_share=_share(debit_map.get(day, 0), peak),
            credit_share=_share(credit_map.get(day, 0), peak),
        )
        for day in days
    )


def _tx_by_msk_day(
    db: Session,
    tx_type: TxType,
    since: datetime,
    reversed_ids: object | None,
) -> dict[date, int]:
    statement = select(Transaction.created_at, Transaction.amount).where(
        Transaction.tx_type == tx_type,
        Transaction.created_at >= since,
    )
    if reversed_ids is not None:
        statement = statement.where(Transaction.id.not_in(reversed_ids))
    rows = db.execute(statement).all()
    totals: dict[date, int] = {}
    for created_at, amount in rows:
        day = as_utc(created_at).astimezone(MSK).date()
        totals[day] = totals.get(day, 0) + int(amount)
    return totals


def _recent_debits(db: Session, *, limit: int) -> tuple[TxView, ...]:
    rows = db.execute(
        select(Transaction, Certificate.public_number, User.full_name)
        .join(Certificate, Transaction.certificate_id == Certificate.id)
        .join(User, Certificate.holder_id == User.id)
        .where(Transaction.tx_type == TxType.debit)
        .order_by(Transaction.id.desc())
        .limit(limit)
    ).all()
    actor_ids = {row[0].actor_id for row in rows if row[0].actor_id is not None}
    names = _user_names(db, actor_ids)
    places = _user_restaurants(db, actor_ids)
    undone = _undone_ids(db, [row[0].id for row in rows])
    return tuple(
        _tx_view(
            tx,
            names.get(tx.actor_id or 0, ""),
            holder_name,
            public_number,
            tx.id in undone,
            None,
            restaurant=tx.restaurant or places.get(tx.actor_id or 0, ""),
        )
        for tx, public_number, holder_name in rows
    )


def _expiring_cards(db: Session, today: date, soon: date, *, limit: int) -> tuple[DashCardRow, ...]:
    rows = db.execute(
        select(Certificate, User.full_name, Segment.name)
        .join(User, Certificate.holder_id == User.id)
        .outerjoin(Segment, Certificate.segment_id == Segment.id)
        .where(
            Certificate.status == CertificateStatus.active,
            Certificate.expires_on.is_not(None),
            Certificate.expires_on >= today,
            Certificate.expires_on <= soon,
            Certificate.balance > 0,
        )
        .order_by(Certificate.expires_on.asc(), Certificate.id.asc())
        .limit(limit)
    ).all()
    return tuple(_dash_card_row(cert, holder_name, segment_name) for cert, holder_name, segment_name in rows)


def _top_balance_cards(db: Session, *, limit: int) -> tuple[DashCardRow, ...]:
    rows = db.execute(
        select(Certificate, User.full_name, Segment.name)
        .join(User, Certificate.holder_id == User.id)
        .outerjoin(Segment, Certificate.segment_id == Segment.id)
        .where(Certificate.status == CertificateStatus.active, Certificate.balance > 0)
        .order_by(Certificate.balance.desc(), Certificate.id.desc())
        .limit(limit)
    ).all()
    return tuple(_dash_card_row(cert, holder_name, segment_name) for cert, holder_name, segment_name in rows)


def _dash_card_row(cert: Certificate, holder_name: str, segment_name: str | None) -> DashCardRow:
    return DashCardRow(
        id=cert.id,
        public_number=cert.public_number,
        holder_name=holder_name,
        segment_name=segment_name or "—",
        balance=cert.balance,
        color=cert.color or "#00A8A8",
        expires_on=cert.expires_on,
    )

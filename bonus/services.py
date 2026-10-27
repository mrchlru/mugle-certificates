"""Выпуск сертификатов, списание и отмена."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bonus.models import (
    AccrualPeriod,
    Certificate,
    CertificatePart,
    CertificateStatus,
    Role,
    Segment,
    Transaction,
    TxType,
    User,
)
from bonus.parts import (
    PartDraft,
    available_balance,
    change_part_balance,
    create_parts_for_certificate,
    ensure_legacy_part,
    list_parts,
    spend_order,
    sync_certificate_totals,
    unwrap_kind_from_segments,
)
from bonus.security import generate_password, hash_password
from bonus.values import MSK, as_utc, format_date, today_msk

UNDO_WINDOW = timedelta(minutes=15)


class BonusError(Exception):
    """Ожидаемый отказ: так делать нельзя, человеку нужно объяснить почему."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class IssuedCertificate:
    """Результат выпуска: кому, какой сертификат и какой пароль показать HR."""

    user: User
    certificate: Certificate
    password: str | None


@dataclass(frozen=True)
class IssueDraft:
    """Данные, из которых собирается сертификат."""

    full_name: str
    login: str
    password: str
    nominal: int
    balance: int | None
    note: str
    expires_on: date | None
    segment_id: int | None = None
    template_id: int | None = None
    color: str = "#00A8A8"
    department: str = ""
    job_title: str = ""
    phone: str = ""
    discount_percent: int = 0
    segment_name: str = ""
    single_use: bool = False
    use_days: str = "1234567"
    accrual_days: str = "12345"
    ledger_sheet: str = "employee"
    company: str = ""
    birth_date: date | None = None
    show_wallet_buttons: bool = False
    template_name: str = ""
    parts: tuple[PartDraft, ...] = ()


def issue_certificate(
    db: Session,
    draft: IssueDraft,
    actor: User | None,
    *,
    sync_external: bool = True,
) -> IssuedCertificate:
    """Создаёт сертификат и, если логина ещё нет, учётную запись сотрудника.

    sync_external=False — без Google Sheets/Wallet (пакетный импорт синхронизирует сам).
    """
    holder, password = _holder_for_issue(db, draft)
    part_drafts = _part_drafts_from_issue(draft)
    if part_drafts:
        total_nominal = sum(item.nominal for item in part_drafts)
        total_balance = sum(
            item.nominal if item.balance is None else item.balance
            for item in part_drafts
        )
        primary = part_drafts[0]
        unwrap = unwrap_kind_from_segments(
            db, [item.segment_id for item in part_drafts]
        )
        segment_id = primary.segment_id
        use_days = (primary.use_days or "1234567")[:16]
        accrual_days = (primary.accrual_days or "12345")[:16]
        single_use = draft.single_use and len(part_drafts) == 1
    else:
        total_nominal, total_balance = _opening_amounts(draft.nominal, draft.balance)
        unwrap = unwrap_kind_from_segments(db, [draft.segment_id])
        segment_id = draft.segment_id
        use_days = (draft.use_days or "1234567")[:16]
        accrual_days = (draft.accrual_days or "12345")[:16]
        single_use = draft.single_use
    certificate = Certificate(
        public_number=_temporary_number(),
        token=_new_token(db),
        short_code=_new_short_code(db),
        holder_id=holder.id,
        segment_id=segment_id,
        template_id=draft.template_id,
        color=draft.color or "#00A8A8",
        discount_percent=normalize_discount(draft.discount_percent),
        single_use=single_use,
        use_days=use_days,
        accrual_days=accrual_days,
        nominal=total_nominal,
        balance=total_balance,
        note=draft.note.strip()[:500],
        ledger_sheet=(draft.ledger_sheet or "employee").strip()[:32],
        company=(draft.company or "").strip()[:200],
        birth_date=draft.birth_date,
        status=CertificateStatus.active,
        expires_on=draft.expires_on,
        unwrap_pending=bool(unwrap),
        show_wallet_buttons=bool(draft.show_wallet_buttons),
        issued_by_id=None if actor is None else actor.id,
    )
    db.add(certificate)
    db.flush()
    certificate.public_number = f"MR-{certificate.id:05d}"
    if part_drafts:
        parts = create_parts_for_certificate(db, certificate, part_drafts)
        db.refresh(certificate)
        for part in parts:
            _add_transaction(
                db,
                certificate_id=certificate.id,
                actor_id=None if actor is None else actor.id,
                tx_type=TxType.issue,
                amount=part.nominal,
                balance_after=certificate.balance,
                comment=f"Выпуск сертификата · {part.segment_name}".strip(" ·"),
                part_id=part.id,
            )
            opening = part.balance
            if opening < part.nominal:
                _add_transaction(
                    db,
                    certificate_id=certificate.id,
                    actor_id=None if actor is None else actor.id,
                    tx_type=TxType.migrate,
                    amount=part.nominal - opening,
                    balance_after=certificate.balance,
                    comment="Остаток перенесён из прежней системы",
                    part_id=part.id,
                )
    else:
        _add_transaction(
            db,
            certificate_id=certificate.id,
            actor_id=None if actor is None else actor.id,
            tx_type=TxType.issue,
            amount=total_nominal,
            balance_after=total_nominal,
            comment="Выпуск сертификата",
        )
        if total_balance < total_nominal:
            _add_transaction(
                db,
                certificate_id=certificate.id,
                actor_id=None if actor is None else actor.id,
                tx_type=TxType.migrate,
                amount=total_nominal - total_balance,
                balance_after=total_balance,
                comment="Остаток перенесён из прежней системы",
            )
    db.commit()
    db.refresh(certificate)
    db.refresh(holder)
    if sync_external:
        _sync_issue(db, certificate, holder, draft.segment_name)
    else:
        from bonus.sheets_queue import enqueue_certificate_ids

        enqueue_certificate_ids(db, [certificate.id])
    return IssuedCertificate(user=holder, certificate=certificate, password=password)


def _part_drafts_from_issue(draft: IssueDraft) -> list[PartDraft]:
    """Собирает части выпуска: явный список или один сегмент как раньше."""
    if draft.parts:
        return [
            PartDraft(
                segment_id=item.segment_id,
                nominal=item.nominal,
                use_days=item.use_days,
                accrual_days=item.accrual_days,
                balance=item.balance,
                sort_order=item.sort_order or index,
            )
            for index, item in enumerate(draft.parts, start=1)
            if item.nominal > 0 or (item.balance is not None and item.balance > 0)
        ]
    if draft.segment_id is None:
        return []
    nominal, balance = _opening_amounts(draft.nominal, draft.balance)
    return [
        PartDraft(
            segment_id=draft.segment_id,
            nominal=nominal,
            use_days=draft.use_days or "1234567",
            accrual_days=draft.accrual_days or "12345",
            balance=balance,
            sort_order=1,
        )
    ]


def debit_certificate(
    db: Session,
    certificate: Certificate,
    actor: User,
    amount: int,
    comment: str,
    *,
    enforce_weekdays: bool = True,
    burn_single_use: bool = True,
    part_id: int | None = None,
) -> Transaction:
    """Списывает бонусы по частям (сначала ежедневная). У разового — сгорание остатка.

    Из админки: enforce_weekdays=False и burn_single_use=False — правка баланса
    без проверки дней использования и без автосгорания остатка.
    part_id — списать только с указанной части (ручная корректировка).
    """
    if apply_expiry_writeoff(db, certificate):
        db.commit()
        db.refresh(certificate)
        _sync_usage(db, certificate, expired=True)
    parts = list_parts(db, certificate.id)
    if not parts:
        ensure_legacy_part(db, certificate)
        parts = list_parts(db, certificate.id)
    _ensure_can_debit(
        certificate,
        amount,
        parts=parts or None,
        enforce_weekdays=enforce_weekdays,
    )
    if certificate.status != CertificateStatus.active:
        raise BonusError("Сертификат заблокирован")
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        raise BonusError("Срок сертификата истёк")

    text = comment.strip() or "Списание бонусов"
    place = actor.restaurant.strip()[:200]

    # Карты без частей (legacy / тесты) — прежнее списание с certificate.balance.
    if not parts:
        changed = _change_balance(db, certificate.id, -amount, require_active=True)
        if not changed:
            db.rollback()
            raise BonusError("Недостаточно бонусов или сертификат уже недоступен")
        db.refresh(certificate)
        last_tx = _add_transaction(
            db,
            certificate_id=certificate.id,
            actor_id=actor.id,
            tx_type=TxType.debit,
            amount=amount,
            balance_after=certificate.balance,
            comment=text[:300],
            restaurant=place,
        )
        if burn_single_use:
            _burn_single_use_remainder(
                db,
                certificate,
                actor_id=actor.id,
                after_debit_id=last_tx.id,
                restaurant=place,
            )
        db.commit()
        db.refresh(last_tx)
        db.refresh(certificate)
        _sync_usage(db, certificate)
        return last_tx

    remaining = amount
    last_tx: Transaction | None = None
    targets = parts
    if part_id is not None:
        targets = [part for part in parts if part.id == part_id]
        if not targets:
            raise BonusError("Часть карты не найдена")
    for part in spend_order(targets):
        if remaining <= 0:
            break
        if enforce_weekdays and part_id is None:
            from bonus.weekdays import allows_weekday

            if not allows_weekday(part.use_days, today_msk().isoweekday()):
                continue
        take = min(part.balance, remaining)
        if take <= 0:
            continue
        if not change_part_balance(db, part.id, -take):
            db.rollback()
            raise BonusError("Недостаточно бонусов или сертификат уже недоступен")
        remaining -= take
        sync_certificate_totals(db, certificate.id)
        db.refresh(certificate)
        label = (part.segment_name or "").strip()
        comment_text = text if len(targets) == 1 else f"{text} · {label}".strip(" ·")
        last_tx = _add_transaction(
            db,
            certificate_id=certificate.id,
            actor_id=actor.id,
            tx_type=TxType.debit,
            amount=take,
            balance_after=certificate.balance,
            comment=comment_text[:300],
            restaurant=place,
            part_id=part.id,
        )
    if remaining > 0 or last_tx is None:
        db.rollback()
        raise BonusError("Недостаточно бонусов или сертификат уже недоступен")
    if burn_single_use:
        _burn_single_use_remainder(
            db,
            certificate,
            actor_id=actor.id,
            after_debit_id=last_tx.id,
            restaurant=place,
        )
    db.commit()
    db.refresh(last_tx)
    db.refresh(certificate)
    _sync_usage(db, certificate)
    return last_tx


def undo_debit(db: Session, tx_id: int, actor: User, reason: str) -> Transaction:
    """Возвращает списанные бонусы. Управляющий успевает это сделать 15 минут."""
    tx = db.get(Transaction, tx_id)
    if tx is None or tx.tx_type != TxType.debit:
        raise BonusError("Списание не найдено")
    _ensure_can_undo(db, tx, actor)
    certificate = db.get(Certificate, tx.certificate_id)
    if certificate is None:
        raise BonusError("Сертификат не найден")
    _restore_single_use_burn(db, certificate, tx, actor.id)
    if tx.part_id is not None:
        if not change_part_balance(db, tx.part_id, tx.amount):
            db.rollback()
            raise BonusError("Не удалось вернуть бонусы")
        sync_certificate_totals(db, certificate.id)
    else:
        changed = _change_balance(db, certificate.id, tx.amount, require_active=False)
        if not changed:
            db.rollback()
            raise BonusError("Не удалось вернуть бонусы")
    db.refresh(certificate)
    reversal = _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=actor.id,
        tx_type=TxType.credit,
        amount=tx.amount,
        balance_after=certificate.balance,
        comment=(reason or "Отмена списания").strip()[:300],
        reversal_of_id=tx.id,
        restaurant=tx.restaurant,
        part_id=tx.part_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BonusError("Это списание уже отменено") from exc
    db.refresh(reversal)
    db.refresh(certificate)
    _sync_usage(db, certificate)
    return reversal


def reset_part_balance(db: Session, part: CertificatePart, comment: str) -> None:
    """Сжигает остаток части и выставляет её баланс равным номиналу."""
    certificate = db.get(Certificate, part.certificate_id)
    if certificate is None:
        raise BonusError("Сертификат не найден")
    text = comment.strip()[:300] or "Автоначисление"
    leftover = part.balance
    if leftover > 0:
        if not change_part_balance(db, part.id, -leftover):
            db.rollback()
            raise BonusError("Не удалось списать остаток перед автоначислением")
        sync_certificate_totals(db, certificate.id)
        db.refresh(certificate)
        _add_transaction(
            db,
            certificate_id=certificate.id,
            actor_id=None,
            tx_type=TxType.burn,
            amount=leftover,
            balance_after=certificate.balance,
            comment="Сгорание остатка перед автоначислением",
            part_id=part.id,
        )
    db.refresh(part)
    target = max(0, part.nominal)
    if part.balance != target:
        delta = target - part.balance
        if not change_part_balance(db, part.id, delta):
            db.rollback()
            raise BonusError("Не удалось выставить лимит автоначисления")
        sync_certificate_totals(db, certificate.id)
        db.refresh(certificate)
    _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=None,
        tx_type=TxType.credit,
        amount=target,
        balance_after=certificate.balance,
        comment=text,
        part_id=part.id,
    )
    db.commit()
    db.refresh(certificate)
    _sync_usage(db, certificate)


def reset_monthly_balance(db: Session, certificate: Certificate, comment: str) -> None:
    """Сброс к номиналу: по первой части или по certificate.balance (legacy)."""
    parts = list_parts(db, certificate.id)
    if not parts:
        ensure_legacy_part(db, certificate)
        parts = list_parts(db, certificate.id)
    if parts:
        reset_part_balance(db, parts[0], comment)
        return
    text = comment.strip()[:300] or "Автоначисление: ежемесячно"
    leftover = certificate.balance
    if leftover > 0:
        changed = _change_balance(db, certificate.id, -leftover, require_active=False)
        if not changed:
            db.rollback()
            raise BonusError("Не удалось списать остаток перед ежемесячным обновлением")
        db.refresh(certificate)
        _add_transaction(
            db,
            certificate_id=certificate.id,
            actor_id=None,
            tx_type=TxType.burn,
            amount=leftover,
            balance_after=certificate.balance,
            comment="Сгорание остатка перед автоначислением",
        )
    target = max(0, certificate.nominal)
    if certificate.balance != target:
        delta = target - certificate.balance
        changed = _change_balance(
            db, certificate.id, delta, require_active=False, grow_nominal=False
        )
        if not changed:
            db.rollback()
            raise BonusError("Не удалось выставить лимит автоначисления")
        db.refresh(certificate)
    _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=None,
        tx_type=TxType.credit,
        amount=target,
        balance_after=certificate.balance,
        comment=text,
    )
    db.commit()
    db.refresh(certificate)
    _sync_usage(db, certificate)


def burn_part_remainder(db: Session, part: CertificatePart, comment: str) -> None:
    """Обнуляет остаток части (ежедневное сгорание в 17:00)."""
    certificate = db.get(Certificate, part.certificate_id)
    if certificate is None:
        raise BonusError("Сертификат не найден")
    text = comment.strip()[:300] or "Сгорание остатка: ежедневно"
    leftover = max(0, part.balance)
    if leftover > 0:
        if not change_part_balance(db, part.id, -leftover):
            db.rollback()
            raise BonusError("Не удалось сжечь дневной остаток")
        sync_certificate_totals(db, certificate.id)
        db.refresh(certificate)
    _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=None,
        tx_type=TxType.burn,
        amount=leftover,
        balance_after=certificate.balance,
        comment=text,
        part_id=part.id,
    )
    db.commit()
    db.refresh(certificate)
    _sync_usage(db, certificate)


def burn_daily_remainder(db: Session, certificate: Certificate, comment: str) -> None:
    """Обнуляет ежедневную часть карты в 17:00 МСК."""
    parts = [
        part
        for part in list_parts(db, certificate.id)
        if part.accrual_period == AccrualPeriod.daily
    ]
    if not parts:
        ensure_legacy_part(db, certificate)
        parts = [
            part
            for part in list_parts(db, certificate.id)
            if part.accrual_period == AccrualPeriod.daily
        ]
    if not parts:
        return
    for part in parts:
        burn_part_remainder(db, part, comment)


def delete_certificate(db: Session, certificate: Certificate, actor: User) -> str:
    """Полностью удаляет карту, журнал и строку в Google Sheets. Только admin."""
    if actor.role != Role.admin:
        raise BonusError("Удалять карты может только главный администратор")
    public_number = certificate.public_number
    ledger_sheet = certificate.ledger_sheet
    holder_id = certificate.holder_id
    certificate_id = certificate.id
    from sqlalchemy import delete as sa_delete

    from bonus.models import LinkDelivery, SheetsSyncJob

    db.execute(
        update(Transaction)
        .where(Transaction.certificate_id == certificate_id)
        .values(reversal_of_id=None, part_id=None)
    )
    db.execute(sa_delete(Transaction).where(Transaction.certificate_id == certificate_id))
    db.execute(
        sa_delete(CertificatePart).where(CertificatePart.certificate_id == certificate_id)
    )
    db.execute(
        sa_delete(SheetsSyncJob).where(SheetsSyncJob.certificate_id == certificate_id)
    )
    db.execute(
        sa_delete(LinkDelivery).where(LinkDelivery.certificate_id == certificate_id)
    )
    certificate.parts.clear()
    db.delete(certificate)
    holder = db.get(User, holder_id)
    if holder is not None and holder.role == Role.employee:
        other = db.scalar(
            select(Certificate.id).where(
                Certificate.holder_id == holder_id,
                Certificate.id != certificate_id,
            )
        )
        if other is None:
            db.delete(holder)
    db.commit()
    from bonus.sheets_sync import delete_certificate_row

    delete_certificate_row(public_number, ledger_sheet)
    return public_number


def credit_certificate(
    db: Session,
    certificate: Certificate,
    actor: User | None,
    amount: int,
    comment: str,
    *,
    grow_nominal: bool = True,
    part_id: int | None = None,
) -> Transaction:
    """Добавляет бонусы к остатку (к выбранной части или к первой).

    Для ручного начисления HR номинал растёт вместе с суммой.
    Автоначисление по сегменту оставляет номинал как первоначальную сумму.
    """
    if amount <= 0:
        raise BonusError("Сумма начисления должна быть больше нуля")
    text = comment.strip() or "Начисление бонусов"
    parts = list_parts(db, certificate.id)
    if not parts:
        ensure_legacy_part(db, certificate)
        parts = list_parts(db, certificate.id)
    target: CertificatePart | None = None
    if part_id is not None:
        target = next((part for part in parts if part.id == part_id), None)
        if target is None:
            raise BonusError("Часть карты не найдена")
    elif parts:
        target = parts[0]
    if target is not None:
        if not change_part_balance(db, target.id, amount):
            db.rollback()
            raise BonusError("Не удалось начислить бонусы")
        if grow_nominal:
            target.nominal = max(0, target.nominal) + amount
            db.flush()
        sync_certificate_totals(db, certificate.id)
    else:
        changed = _change_balance(
            db,
            certificate.id,
            amount,
            require_active=False,
            grow_nominal=grow_nominal,
        )
        if not changed:
            db.rollback()
            raise BonusError("Не удалось начислить бонусы")
    db.refresh(certificate)
    tx = _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=None if actor is None else actor.id,
        tx_type=TxType.credit,
        amount=amount,
        balance_after=certificate.balance,
        comment=text[:300],
        part_id=None if target is None else target.id,
    )
    db.commit()
    db.refresh(tx)
    db.refresh(certificate)
    _sync_usage(db, certificate)
    return tx


def set_blocked(db: Session, certificate: Certificate, actor: User, blocked: bool) -> None:
    """Закрывает или снова открывает списания по сертификату."""
    certificate.status = CertificateStatus.blocked if blocked else CertificateStatus.active
    _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=actor.id,
        tx_type=TxType.block if blocked else TxType.unblock,
        amount=0,
        balance_after=certificate.balance,
        comment="Сертификат заблокирован" if blocked else "Сертификат снова активен",
    )
    db.commit()
    db.refresh(certificate)
    _sync_usage(db, certificate)


def rename_holder(db: Session, user: User, full_name: str) -> None:
    """Исправляет имя на карточке."""
    update_holder_profile(db, user, full_name=full_name)


def update_holder_profile(
    db: Session,
    user: User,
    *,
    full_name: str,
    department: str = "",
    job_title: str = "",
    phone: str = "",
) -> None:
    """Обновляет ФИО и внутренние поля сотрудника."""
    cleaned = " ".join(full_name.split())
    if len(cleaned) < 2:
        raise BonusError("Укажите имя и фамилию")
    user.full_name = cleaned[:200]
    user.department = _clean_optional(department, 120)
    user.job_title = _clean_optional(job_title, 120)
    user.phone = _clean_optional(phone, 40)
    db.commit()


def update_certificate_profile(
    db: Session,
    certificate: Certificate,
    *,
    company: str,
    birth_date: date | None,
) -> None:
    """Обновляет ООО и дату рождения на карте."""
    from bonus.ledger import ledger_needs_birth_date, parse_ledger_sheet

    cleaned_company = " ".join(company.split())
    if not cleaned_company:
        raise BonusError("Укажите ООО")
    sheet = parse_ledger_sheet(certificate.ledger_sheet)
    if ledger_needs_birth_date(sheet) and birth_date is None:
        raise BonusError("Укажите дату рождения")
    certificate.company = cleaned_company[:200]
    certificate.birth_date = birth_date
    db.commit()
    db.refresh(certificate)


def set_certificate_expiry(db: Session, certificate: Certificate, expires_on: date | None) -> None:
    """Задаёт или снимает срок действия сертификата."""
    certificate.expires_on = expires_on
    db.flush()
    expired = apply_expiry_writeoff(db, certificate)
    db.commit()
    db.refresh(certificate)
    _sync_usage(db, certificate, expired=expired)


def extend_certificate_days(db: Session, certificate: Certificate, days: int) -> date:
    """Продлевает срок на N дней от текущего (или от сегодня, если срок прошёл/пуст)."""
    if days < 1 or days > 3600:
        raise BonusError("Срок продления — от 1 до 3600 дней")
    base = certificate.expires_on
    today = today_msk()
    if base is None or base < today:
        base = today
    new_expires = base + timedelta(days=days)
    set_certificate_expiry(db, certificate, new_expires)
    return new_expires


def extend_certificates_days(
    db: Session,
    certificate_ids: list[int],
    days: int,
) -> tuple[int, list[str]]:
    """Продлевает несколько карт. Возвращает (успешно, публичные номера)."""
    unique_ids = sorted({int(item) for item in certificate_ids if item})
    if not unique_ids:
        raise BonusError("Выберите хотя бы одну карту")
    if days < 1 or days > 3600:
        raise BonusError("Срок продления — от 1 до 3600 дней")
    done: list[str] = []
    for certificate_id in unique_ids:
        certificate = db.get(Certificate, certificate_id)
        if certificate is None:
            continue
        extend_certificate_days(db, certificate, days)
        done.append(certificate.public_number)
    if not done:
        raise BonusError("Не удалось продлить выбранные карты")
    return len(done), done


def apply_expiry_writeoff(db: Session, certificate: Certificate) -> bool:
    """Если срок прошёл — списывает весь остаток в журнал (по частям)."""
    if certificate.expires_on is None or certificate.expires_on >= today_msk():
        return False
    parts = list_parts(db, certificate.id)
    total = sum(max(0, part.balance) for part in parts) if parts else certificate.balance
    if total <= 0:
        return False
    until = format_date(certificate.expires_on)
    if parts:
        for part in parts:
            leftover = max(0, part.balance)
            if leftover <= 0:
                continue
            part.balance = 0
            _add_transaction(
                db,
                certificate_id=certificate.id,
                actor_id=None,
                tx_type=TxType.expire,
                amount=leftover,
                balance_after=0,
                comment=f"Списано: истёк срок (годен до {until}) · {part.segment_name}",
                part_id=part.id,
            )
        sync_certificate_totals(db, certificate.id)
    else:
        certificate.balance = 0
        _add_transaction(
            db,
            certificate_id=certificate.id,
            actor_id=None,
            tx_type=TxType.expire,
            amount=total,
            balance_after=0,
            comment=f"Списано: истёк срок (годен до {until})",
        )
    db.flush()
    return True


def apply_due_expiries(db: Session) -> int:
    """Проходит по просроченным сертификатам с остатком и списывает их."""
    rows = db.scalars(
        select(Certificate).where(
            Certificate.expires_on.is_not(None),
            Certificate.expires_on < today_msk(),
            Certificate.balance > 0,
        )
    ).all()
    changed = 0
    expired_cards: list[Certificate] = []
    for certificate in rows:
        if apply_expiry_writeoff(db, certificate):
            changed += 1
            expired_cards.append(certificate)
    if changed:
        db.commit()
        for certificate in expired_cards:
            _sync_usage(db, certificate, expired=True)
    return changed


def change_password(db: Session, user: User, current: str, new_password: str) -> None:
    """Меняет свой пароль, если текущий введён верно."""
    from bonus.security import verify_password

    if not verify_password(current, user.password_hash):
        raise BonusError("Текущий пароль не совпадает")
    if len(new_password) < 8:
        raise BonusError("Новый пароль — минимум 8 символов")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()


def create_staff(
    db: Session,
    *,
    first_name: str,
    last_name: str,
    phone: str,
    password: str,
    role: Role,
    restaurant: str,
    login: str = "",
) -> tuple[User, str]:
    """Заводит управляющего, HR или HRD для админки / кассы."""
    if role == Role.employee:
        raise BonusError("Сотрудник появляется вместе с сертификатом")
    if role == Role.admin:
        raise BonusError("Главного администратора создают только через переменные окружения")
    if role not in {Role.manager, Role.hr, Role.hrd}:
        raise BonusError("Неизвестная роль")
    first = " ".join(first_name.split())
    last = " ".join(last_name.split())
    if len(first) < 1 or len(last) < 1:
        raise BonusError("Укажите имя и фамилию")
    place = restaurant.strip()
    if role == Role.manager and place == "":
        raise BonusError("Укажите ресторан для управляющего")
    phone_clean = _clean_optional(phone, 40)
    cleaned_login = _normalize_login(login) if login.strip() else _unique_login(db, _login_from_staff(last, phone_clean))
    if db.scalar(select(User.id).where(User.login == cleaned_login)) is not None:
        raise BonusError("Такой логин уже есть")
    plain = password.strip() or generate_password()
    if len(plain) < 8:
        raise BonusError("Пароль — минимум 8 символов")
    user = User(
        full_name=_clean_name(f"{last} {first}"),
        login=cleaned_login,
        password_hash=hash_password(plain),
        role=role,
        restaurant=place[:200],
        phone=phone_clean,
        is_active=True,
        must_change_password=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, plain


def _login_from_staff(last_name: str, phone: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    if len(digits) >= 4:
        return f"mgr{digits[-4:]}"
    base = _login_from_name(last_name)
    return base if base else "manager"


def update_staff(
    db: Session,
    user: User,
    *,
    first_name: str,
    last_name: str,
    phone: str,
    role: Role,
    restaurant: str,
    login: str,
    password: str = "",
) -> User:
    """Меняет данные управляющего, HR или HRD, опционально задаёт новый пароль."""
    _assert_editable_staff(user)
    if role == Role.employee:
        raise BonusError("Нельзя сделать этого пользователя обычным сотрудником")
    if role == Role.admin:
        raise BonusError("Главного администратора создают только через переменные окружения")
    if role not in {Role.manager, Role.hr, Role.hrd}:
        raise BonusError("Неизвестная роль")
    first = " ".join(first_name.split())
    last = " ".join(last_name.split())
    if len(first) < 1 or len(last) < 1:
        raise BonusError("Укажите имя и фамилию")
    place = restaurant.strip()
    if role == Role.manager and place == "":
        raise BonusError("Укажите ресторан для управляющего")
    if user.role == Role.hr and role != Role.hr:
        _ensure_other_hr(db, user.id)
    cleaned_login = _normalize_login(login)
    twin = db.scalar(select(User.id).where(User.login == cleaned_login, User.id != user.id))
    if twin is not None:
        raise BonusError("Такой логин уже есть")
    plain = password.strip()
    if plain and len(plain) < 8:
        raise BonusError("Пароль — минимум 8 символов")
    user.full_name = _clean_name(f"{last} {first}")
    user.login = cleaned_login
    user.role = role
    user.restaurant = place[:200]
    user.phone = _clean_optional(phone, 40)
    if plain:
        user.password_hash = hash_password(plain)
        user.must_change_password = True
    db.commit()
    db.refresh(user)
    return user


def set_user_active(db: Session, user: User, active: bool) -> None:
    """Отключает доступ, не стирая журнал. Последнего HR и админа отключить нельзя."""
    if user.role == Role.admin:
        raise BonusError("Главного администратора отключают только через переменные окружения")
    if user.role == Role.hr and not active:
        _ensure_other_hr(db, user.id)
    user.is_active = active
    db.commit()


def _assert_editable_staff(user: User) -> None:
    """Запрещает править сотрудника с картой и главного администратора в UI."""
    if user.role == Role.employee:
        raise BonusError("Сотрудник с сертификатом здесь не редактируется")
    if user.role == Role.admin:
        raise BonusError("Главного администратора меняют только через переменные окружения")


def _ensure_other_hr(db: Session, user_id: int) -> None:
    """Проверяет, что в системе останется хотя бы один активный HR."""
    others = db.scalar(
        select(User.id).where(User.role == Role.hr, User.is_active.is_(True), User.id != user_id)
    )
    if others is None:
        raise BonusError("Нельзя убрать последнего сотрудника HR")


def find_certificate(db: Session, raw: str) -> Certificate | None:
    """Ищет сертификат по ссылке из QR, короткому коду или номеру MR-00042."""
    extracted = _extract_lookup(raw)
    if extracted == "":
        return None
    by_token = db.scalar(select(Certificate).where(Certificate.token == extracted))
    if by_token is not None:
        return _ready_certificate(db, by_token)
    compact = extracted.replace("-", "").replace(" ", "")
    by_short = db.scalar(select(Certificate).where(Certificate.short_code == compact.upper()))
    if by_short is None:
        digits_only = "".join(char for char in compact if char.isdigit())
        if digits_only != "":
            by_short = db.scalar(select(Certificate).where(Certificate.short_code == digits_only))
    if by_short is not None:
        return _ready_certificate(db, by_short)
    public = _public_number(compact.upper())
    if public == "":
        return None
    found = db.scalar(select(Certificate).where(Certificate.public_number == public))
    if found is None:
        return None
    return _ready_certificate(db, found)


def _ready_certificate(db: Session, certificate: Certificate) -> Certificate:
    if apply_expiry_writeoff(db, certificate):
        db.commit()
        db.refresh(certificate)
    return certificate


def _holder_for_issue(db: Session, draft: IssueDraft) -> tuple[User, str | None]:
    login = draft.login.strip()
    if login == "":
        login = _unique_login(db, _login_from_name(draft.full_name))
    else:
        login = _normalize_login(login)
    existing = db.scalar(select(User).where(User.login == login))
    if existing is None:
        user = User(
            full_name=_clean_name(draft.full_name),
            login=login,
            password_hash=hash_password(generate_password()),
            role=Role.employee,
            restaurant="",
            department=_clean_optional(draft.department, 120),
            job_title=_clean_optional(draft.job_title, 120),
            phone=_clean_optional(draft.phone, 40),
            is_active=True,
        )
        db.add(user)
        db.flush()
        return user, None
    if existing.role != Role.employee:
        raise BonusError("Этот логин занят управляющим или HR")
    if not existing.is_active:
        raise BonusError("Учётная запись сотрудника отключена")
    name = _clean_name(draft.full_name)
    if name != "":
        existing.full_name = name
    _apply_holder_meta(existing, draft)
    return existing, None


def _apply_holder_meta(user: User, draft: IssueDraft) -> None:
    """Пишет отдел, должность и телефон с черновика выпуска."""
    user.department = _clean_optional(draft.department, 120)
    user.job_title = _clean_optional(draft.job_title, 120)
    user.phone = _clean_optional(draft.phone, 40)


def _clean_optional(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _opening_amounts(nominal: int | None, balance: int | None) -> tuple[int, int]:
    if nominal is None and balance is None:
        raise BonusError("Укажите номинал или остаток")
    if nominal is None:
        nominal = balance
    if balance is None:
        balance = nominal
    assert nominal is not None and balance is not None
    if nominal <= 0 and balance <= 0:
        raise BonusError("Сумма сертификата должна быть больше нуля")
    if nominal < balance:
        nominal = balance
    if nominal <= 0:
        nominal = balance
    return nominal, balance


def _ensure_can_debit(
    certificate: Certificate,
    amount: int,
    *,
    parts: list[CertificatePart] | None = None,
    enforce_weekdays: bool = True,
) -> None:
    from bonus.weekdays import allows_weekday, days_label

    if amount <= 0:
        raise BonusError("Сумма списания должна быть больше нуля")
    if certificate.status != CertificateStatus.active:
        raise BonusError("Сертификат заблокирован")
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        raise BonusError("Срок сертификата истёк")
    if parts is not None:
        spendable = available_balance(parts, enforce_weekdays=enforce_weekdays)
        if enforce_weekdays and spendable <= 0 and certificate.balance > 0:
            labels = [
                f"{part.segment_name}: {days_label(part.use_days)}" for part in parts
            ]
            raise BonusError(
                "Карта сегодня не действует. Дни использования: " + "; ".join(labels)
            )
    else:
        if enforce_weekdays and not allows_weekday(
            certificate.use_days, today_msk().isoweekday()
        ):
            raise BonusError(
                "Карта сегодня не действует. Дни использования: "
                f"{days_label(certificate.use_days)}"
            )
        spendable = certificate.balance
    if spendable < amount:
        raise BonusError("На сертификате меньше бонусов, чем вы списываете")


def _ensure_can_undo(db: Session, tx: Transaction, actor: User) -> None:
    if _is_reversed(db, tx.id):
        raise BonusError("Это списание уже отменено")
    if actor.role in {Role.admin, Role.hr, Role.hrd}:
        return
    if actor.role != Role.manager or tx.actor_id != actor.id:
        raise BonusError("Можно отменить только своё списание")
    age = datetime.now(timezone.utc) - as_utc(tx.created_at)
    if age > UNDO_WINDOW:
        raise BonusError("Прошло больше 15 минут — отмену сделает HR")


def _is_reversed(db: Session, tx_id: int) -> bool:
    found = db.scalar(select(Transaction.id).where(Transaction.reversal_of_id == tx_id))
    return found is not None


def _change_balance(
    db: Session,
    certificate_id: int,
    delta: int,
    *,
    require_active: bool,
    grow_nominal: bool = False,
) -> bool:
    statement = update(Certificate).where(Certificate.id == certificate_id)
    if delta < 0:
        statement = statement.where(Certificate.balance >= -delta)
    if require_active:
        statement = statement.where(Certificate.status == CertificateStatus.active)
        statement = statement.where(
            (Certificate.expires_on.is_(None)) | (Certificate.expires_on >= today_msk())
        )
    values: dict[str, object] = {"balance": Certificate.balance + delta}
    if grow_nominal and delta > 0:
        values["nominal"] = Certificate.nominal + delta
    result = db.execute(statement.values(**values))
    return result.rowcount == 1


def _add_transaction(
    db: Session,
    *,
    certificate_id: int,
    actor_id: int | None,
    tx_type: TxType,
    amount: int,
    balance_after: int,
    comment: str,
    reversal_of_id: int | None = None,
    restaurant: str = "",
    order_ref: str = "",
    cheque_detail: str = "",
    part_id: int | None = None,
) -> Transaction:
    tx = Transaction(
        certificate_id=certificate_id,
        part_id=part_id,
        actor_id=actor_id,
        tx_type=tx_type,
        amount=amount,
        balance_after=balance_after,
        comment=comment,
        restaurant=restaurant.strip()[:200],
        order_ref=order_ref.strip()[:80],
        cheque_detail=cheque_detail.strip()[:4000],
        reversal_of_id=reversal_of_id,
    )
    db.add(tx)
    db.flush()
    return tx


def normalize_discount(value: int) -> int:
    """Проверяет отображаемый процент на карточке (не автоскидка в заказе)."""
    if value < 0 or value > 100:
        raise BonusError("Процент — целое число от 0 до 100")
    return value


def _burn_single_use_remainder(
    db: Session,
    certificate: Certificate,
    *,
    actor_id: int | None,
    after_debit_id: int,
    restaurant: str = "",
) -> Transaction | None:
    """После первого списания одноразового сертификата обнуляет остаток."""
    if not certificate.single_use or certificate.balance <= 0:
        return None
    amount = certificate.balance
    for part in list_parts(db, certificate.id):
        if part.balance > 0:
            part.balance = 0
    certificate.balance = 0
    sync_certificate_totals(db, certificate.id)
    return _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=actor_id,
        tx_type=TxType.burn,
        amount=amount,
        balance_after=0,
        comment=f"Списано: одноразовый сертификат · после #{after_debit_id}",
        restaurant=restaurant,
    )


def _restore_single_use_burn(
    db: Session,
    certificate: Certificate,
    debit: Transaction,
    actor_id: int | None,
) -> None:
    """При отмене списания возвращает и сожжённый остаток одноразовой карты."""
    burn = db.scalar(
        select(Transaction)
        .where(
            Transaction.certificate_id == certificate.id,
            Transaction.tx_type == TxType.burn,
            Transaction.id > debit.id,
            Transaction.comment.contains(f"#{debit.id}"),
        )
        .order_by(Transaction.id)
        .limit(1)
    )
    if burn is None or _is_reversed(db, burn.id):
        return
    changed = _change_balance(db, certificate.id, burn.amount, require_active=False)
    if not changed:
        raise BonusError("Не удалось вернуть сгоревший остаток")
    db.refresh(certificate)
    _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=actor_id,
        tx_type=TxType.credit,
        amount=burn.amount,
        balance_after=certificate.balance,
        comment="Возврат сгоревшего остатка одноразового сертификата",
        reversal_of_id=burn.id,
        restaurant=burn.restaurant,
    )


@dataclass(frozen=True)
class IikoDebitResult:
    """Результат списания для плагина iiko: сколько списали и сколько осталось доплатить."""

    transaction: Transaction
    paid: int
    remainder: int
    balance_after: int
    replayed: bool = False


def lookup_for_iiko(db: Session, code: str) -> Certificate:
    """Находит сертификат по QR/коду для кассы iiko."""
    certificate = find_certificate(db, code)
    if certificate is None:
        raise BonusError("Сертификат не найден")
    expired = apply_expiry_writeoff(db, certificate)
    db.commit()
    db.refresh(certificate)
    if expired:
        _sync_usage(db, certificate, expired=True)
    return certificate


def debit_for_iiko(
    db: Session,
    certificate: Certificate,
    *,
    amount: int,
    external_id: str,
    restaurant: str = "",
    comment: str = "",
    order_ref: str = "",
    cheque_detail: str = "",
) -> IikoDebitResult:
    """Списывает бонусы до суммы заказа; при нехватке — весь остаток. Идемпотентно по external_id."""
    marker = _iiko_marker(external_id)
    existing = _find_iiko_debit(db, marker)
    if existing is not None and not _is_reversed(db, existing.id):
        unpaid = max(0, amount - existing.amount)
        return IikoDebitResult(
            transaction=existing,
            paid=existing.amount,
            remainder=unpaid,
            balance_after=existing.balance_after,
            replayed=True,
        )
    if apply_expiry_writeoff(db, certificate):
        db.commit()
        db.refresh(certificate)
        _sync_usage(db, certificate, expired=True)
    if certificate.status != CertificateStatus.active:
        raise BonusError("Сертификат заблокирован")
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        raise BonusError("Срок сертификата истёк")
    if amount <= 0:
        raise BonusError("Сумма списания должна быть больше нуля")
    if certificate.balance <= 0:
        raise BonusError("На сертификате нет бонусов")
    paid = min(amount, certificate.balance)
    changed = _change_balance(db, certificate.id, -paid, require_active=True)
    if not changed:
        db.rollback()
        raise BonusError("Недостаточно бонусов или сертификат уже недоступен")
    db.refresh(certificate)
    note = f"{marker} {(comment or 'iiko').strip()}"[:300]
    place = restaurant.strip()[:200]
    tx = _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=None,
        tx_type=TxType.debit,
        amount=paid,
        balance_after=certificate.balance,
        comment=note,
        restaurant=place,
        order_ref=order_ref,
        cheque_detail=cheque_detail,
    )
    _burn_single_use_remainder(
        db,
        certificate,
        actor_id=None,
        after_debit_id=tx.id,
        restaurant=place,
    )
    db.commit()
    db.refresh(certificate)
    db.refresh(tx)
    _sync_usage(db, certificate)
    return IikoDebitResult(
        transaction=tx,
        paid=paid,
        remainder=max(0, amount - paid),
        balance_after=certificate.balance,
        replayed=False,
    )


def refund_for_iiko(db: Session, external_id: str, reason: str = "") -> Transaction:
    """Возвращает бонусы по операции iiko (отмена оплаты на кассе)."""
    marker = _iiko_marker(external_id)
    tx = _find_iiko_debit(db, marker)
    if tx is None:
        raise BonusError("Операция iiko не найдена")
    existing_reversal = _reversal_of(db, tx.id)
    if existing_reversal is not None:
        return existing_reversal
    certificate = db.get(Certificate, tx.certificate_id)
    if certificate is None:
        raise BonusError("Сертификат не найден")
    changed = _change_balance(db, certificate.id, tx.amount, require_active=False)
    if not changed:
        db.rollback()
        raise BonusError("Не удалось вернуть бонусы")
    db.refresh(certificate)
    reversal = _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=None,
        tx_type=TxType.credit,
        amount=tx.amount,
        balance_after=certificate.balance,
        comment=(reason or "Возврат iiko").strip()[:300],
        reversal_of_id=tx.id,
        restaurant=tx.restaurant,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        again = _reversal_of(db, tx.id)
        if again is not None:
            return again
        raise BonusError("Эта операция уже отменена")
    db.refresh(reversal)
    db.refresh(certificate)
    _sync_usage(db, certificate)
    return reversal


def _reversal_of(db: Session, tx_id: int) -> Transaction | None:
    """Находит проводку-отмену для списания, если она уже есть."""
    return db.scalar(
        select(Transaction).where(Transaction.reversal_of_id == tx_id).limit(1)
    )


def _iiko_marker(external_id: str) -> str:
    cleaned = " ".join(external_id.split())
    if cleaned == "":
        raise BonusError("Нужен идентификатор операции iiko")
    return f"iiko:{cleaned[:80]}"


def _find_iiko_debit(db: Session, marker: str) -> Transaction | None:
    return db.scalar(
        select(Transaction)
        .where(Transaction.tx_type == TxType.debit, Transaction.comment.startswith(marker))
        .order_by(Transaction.id.desc())
        .limit(1)
    )


def _new_token(db: Session) -> str:
    import secrets

    for _ in range(8):
        token = secrets.token_urlsafe(24)
        if db.scalar(select(Certificate.id).where(Certificate.token == token)) is None:
            return token
    raise BonusError("Не удалось выдать код сертификата")


def _new_short_code(db: Session) -> str:
    """Восемь цифр без ведущего нуля — удобно для Code128 и сканеров кассы."""
    import secrets

    for _ in range(24):
        code = f"{secrets.randbelow(90_000_000) + 10_000_000:08d}"
        if db.scalar(select(Certificate.id).where(Certificate.short_code == code)) is None:
            return code
    raise BonusError("Не удалось выдать короткий код")


def _temporary_number() -> str:
    import secrets

    return f"TMP-{secrets.token_hex(4)}"


def _unwrap_kind_for_segment(db: Session, segment_id: int | None) -> str:
    """Вид распаковки сегмента: '' | 'birthday' | 'gift'."""
    if segment_id is None:
        return ""
    segment = db.get(Segment, segment_id)
    if segment is None:
        return ""
    kind = (getattr(segment, "unwrap_kind", None) or "").strip().lower()
    if kind in {"birthday", "gift"}:
        return kind
    # Совместимость со старым boolean birthday_unwrap.
    if getattr(segment, "birthday_unwrap", False):
        return "birthday"
    return ""


def _normalize_login(login: str) -> str:
    import re

    cleaned = login.strip().casefold()
    if re.fullmatch(r"[\w.\-]{2,64}", cleaned, flags=re.UNICODE) is None:
        raise BonusError("Логин: 2–64 символа, без пробелов")
    return cleaned


def _clean_name(full_name: str) -> str:
    cleaned = " ".join(full_name.split())
    if len(cleaned) < 2:
        raise BonusError("Укажите имя и фамилию")
    return cleaned[:200]


def _login_from_name(full_name: str) -> str:
    first = _clean_name(full_name).split()[0]
    mapped = "".join(_TRANSLIT.get(char, char) for char in first.casefold())
    ascii_only = "".join(char for char in mapped if char.isascii() and char.isalnum())
    return (ascii_only or "user")[:20]


def _unique_login(db: Session, base: str) -> str:
    candidate = base
    number = 2
    while db.scalar(select(User.id).where(User.login == candidate)) is not None:
        candidate = f"{base}{number}"
        number += 1
    return candidate


def _extract_lookup(raw: str) -> str:
    from urllib.parse import unquote

    text = unquote(raw).strip()
    for marker in ("/m/", "/c/"):
        if marker in text:
            text = text.split(marker)[-1]
            break
    text = text.split("?")[0].split("#")[0].strip().strip("/")
    return text


def _public_number(compact: str) -> str:
    if not compact.startswith("MR"):
        return ""
    digits = compact[2:]
    if not digits.isdigit():
        return ""
    return f"MR-{int(digits):05d}"


def _sync_issue(
    db: Session,
    certificate: Certificate,
    holder: User,
    segment_name: str,
) -> None:
    """Ставит карту в очередь Sheets и пишет во внешние системы в фоне.

    HTTP-ответ после выпуска не ждёт Google Sheets / Wallet — страница карты
    открывается сразу, статус синка показывается в блоке ссылки.
    """
    del holder, segment_name  # сегмент подтянем в фоновом потоке из БД
    from bonus.sheets_queue import enqueue_certificate_ids

    enqueue_certificate_ids(db, [certificate.id])
    _start_issue_external_sync(certificate.id)


def _start_issue_external_sync(certificate_id: int) -> None:
    """Запускает фоновую запись выпуска в Sheets и Wallet."""
    import threading

    thread = threading.Thread(
        target=_run_issue_external_sync,
        args=(certificate_id,),
        daemon=True,
        name=f"issue-sync-{certificate_id}",
    )
    thread.start()


def _run_issue_external_sync(certificate_id: int) -> None:
    """Пишет одну только что выписанную карту в Sheets и Wallet вне запроса."""
    import logging

    from bonus.db import open_session
    from bonus.sheets_queue import mark_certificate_job
    from bonus.sheets_sync import sheets_enabled, sync_certificate_issued

    logger = logging.getLogger("bonus.services")
    db = open_session()
    try:
        certificate = db.get(Certificate, certificate_id)
        if certificate is None or certificate.holder is None:
            mark_certificate_job(db, certificate_id, "failed", "Сертификат не найден")
            return
        holder = certificate.holder
        segment_name = ""
        if certificate.segment is not None:
            segment_name = (certificate.segment.name or "").strip()
        if not sheets_enabled():
            mark_certificate_job(db, certificate_id, "done")
        else:
            ok = sync_certificate_issued(
                certificate, holder, segment_name=segment_name
            )
            if ok:
                mark_certificate_job(db, certificate_id, "done")
            # иначе job остаётся pending — доберёт воркер очереди
        _sync_wallet(certificate, holder, expired=False)
    except Exception:
        logger.exception(
            "Фоновая синхронизация выпуска не удалась для certificate_id=%s",
            certificate_id,
        )
    finally:
        db.close()


def _sync_usage(
    db: Session,
    certificate: Certificate,
    *,
    expired: bool = False,
) -> None:
    """Пишет карту и расходы в Google Sheets по журналу (без отменённых списаний)."""
    from bonus.sheets_sync import SheetUsageEntry, sync_certificate_usage

    holder = certificate.holder
    segment_name = ""
    if certificate.segment is not None:
        segment_name = (certificate.segment.name or "").strip()
    usages: list[SheetUsageEntry] = []
    if not expired:
        usages = _sheet_usages(db, certificate.id)
    sync_certificate_usage(
        certificate,
        holder,
        usages=usages,
        segment_name=segment_name,
        expired=expired,
    )
    _sync_wallet(certificate, holder, expired=expired)


def _sync_wallet(
    certificate: Certificate,
    holder: User | None,
    *,
    expired: bool = False,
) -> None:
    """Обновляет Loyalty Object в Google Wallet; сбои не ломают кассу."""
    from bonus.config import get_settings
    from bonus.google_wallet import sync_certificate

    base_url = get_settings().app_public_url.strip().rstrip("/")
    sync_certificate(certificate, holder, base_url=base_url, expired=expired)


def _sheet_usages(db: Session, certificate_id: int) -> list:
    """Неотменённые списания карты для колонок Google Sheets."""
    from bonus.sheets_sync import SheetUsageEntry

    reversed_ids = select(Transaction.reversal_of_id).where(
        Transaction.reversal_of_id.is_not(None)
    )
    rows = db.scalars(
        select(Transaction)
        .where(
            Transaction.certificate_id == certificate_id,
            Transaction.tx_type == TxType.debit,
            Transaction.id.not_in(reversed_ids),
        )
        .order_by(Transaction.id)
    ).all()
    return [
        SheetUsageEntry(
            used_on=as_utc(tx.created_at).astimezone(MSK).date(),
            amount_kopecks=tx.amount,
            restaurant=tx.restaurant or "",
            order_ref=(tx.order_ref or "").strip(),
        )
        for tx in rows
    ]


def push_certificate_to_sheets(db: Session, certificate: Certificate) -> None:
    """Дозаписывает карту и все расходы из журнала в Google Sheets."""
    _sync_usage(db, certificate)


def enqueue_certificates_for_sheets(db: Session, certificate_ids: list[int]) -> int:
    """Ставит карты в фоновую очередь Google Sheets."""
    from bonus.sheets_queue import enqueue_certificate_ids

    return enqueue_certificate_ids(db, certificate_ids)


def list_active_certificate_ids(db: Session) -> list[int]:
    """ID активных карт для досинхронизации в Google Sheets."""
    return list(
        db.scalars(
            select(Certificate.id)
            .where(Certificate.status == CertificateStatus.active)
            .order_by(Certificate.id)
        ).all()
    )


_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

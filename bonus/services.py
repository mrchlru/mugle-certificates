"""Выпуск сертификатов, списание и отмена."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bonus.models import Certificate, CertificateStatus, Role, Transaction, TxType, User
from bonus.security import generate_password, hash_password
from bonus.values import as_utc, today_msk

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


def issue_certificate(db: Session, draft: IssueDraft, actor: User | None) -> IssuedCertificate:
    """Создаёт сертификат и, если логина ещё нет, учётную запись сотрудника."""
    holder, password = _holder_for_issue(db, draft)
    nominal, balance = _opening_amounts(draft.nominal, draft.balance)
    certificate = Certificate(
        public_number=_temporary_number(),
        token=_new_token(db),
        short_code=_new_short_code(db),
        holder_id=holder.id,
        segment_id=draft.segment_id,
        template_id=draft.template_id,
        color=draft.color or "#00A8A8",
        nominal=nominal,
        balance=balance,
        note=draft.note.strip()[:500],
        status=CertificateStatus.active,
        expires_on=draft.expires_on,
        issued_by_id=None if actor is None else actor.id,
    )
    db.add(certificate)
    db.flush()
    certificate.public_number = f"MR-{certificate.id:05d}"
    _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=None if actor is None else actor.id,
        tx_type=TxType.issue,
        amount=nominal,
        balance_after=nominal,
        comment="Выпуск сертификата",
    )
    if balance < nominal:
        _add_transaction(
            db,
            certificate_id=certificate.id,
            actor_id=None if actor is None else actor.id,
            tx_type=TxType.migrate,
            amount=nominal - balance,
            balance_after=balance,
            comment="Остаток перенесён из прежней системы",
        )
    db.commit()
    db.refresh(certificate)
    db.refresh(holder)
    return IssuedCertificate(user=holder, certificate=certificate, password=password)


def debit_certificate(
    db: Session,
    certificate: Certificate,
    actor: User,
    amount: int,
    comment: str,
) -> Transaction:
    """Списывает бонусы. Повторный клик не уводит остаток ниже нуля."""
    _ensure_can_debit(certificate, amount)
    changed = _change_balance(db, certificate.id, -amount, require_active=True)
    if not changed:
        db.rollback()
        raise BonusError("Недостаточно бонусов или сертификат уже недоступен")
    db.refresh(certificate)
    tx = _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=actor.id,
        tx_type=TxType.debit,
        amount=amount,
        balance_after=certificate.balance,
        comment=comment.strip()[:300],
    )
    db.commit()
    db.refresh(tx)
    return tx


def undo_debit(db: Session, tx_id: int, actor: User, reason: str) -> Transaction:
    """Возвращает списанные бонусы. Управляющий успевает это сделать 15 минут."""
    tx = db.get(Transaction, tx_id)
    if tx is None or tx.tx_type != TxType.debit:
        raise BonusError("Списание не найдено")
    _ensure_can_undo(db, tx, actor)
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
        actor_id=actor.id,
        tx_type=TxType.credit,
        amount=tx.amount,
        balance_after=certificate.balance,
        comment=(reason or "Отмена списания").strip()[:300],
        reversal_of_id=tx.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BonusError("Это списание уже отменено") from exc
    db.refresh(reversal)
    return reversal


def credit_certificate(
    db: Session,
    certificate: Certificate,
    actor: User,
    amount: int,
    comment: str,
) -> Transaction:
    """Добавляет бонусы к остатку. Номинал растёт на ту же сумму, чтобы карточка не врала."""
    if amount <= 0:
        raise BonusError("Сумма начисления должна быть больше нуля")
    text = comment.strip() or "Начисление бонусов"
    changed = _change_balance(db, certificate.id, amount, require_active=False, grow_nominal=True)
    if not changed:
        db.rollback()
        raise BonusError("Не удалось начислить бонусы")
    db.refresh(certificate)
    tx = _add_transaction(
        db,
        certificate_id=certificate.id,
        actor_id=actor.id,
        tx_type=TxType.credit,
        amount=amount,
        balance_after=certificate.balance,
        comment=text[:300],
    )
    db.commit()
    db.refresh(tx)
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


def rename_holder(db: Session, user: User, full_name: str) -> None:
    """Исправляет имя на карточке."""
    cleaned = " ".join(full_name.split())
    if len(cleaned) < 2:
        raise BonusError("Укажите имя и фамилию")
    user.full_name = cleaned[:200]
    db.commit()


def set_certificate_expiry(db: Session, certificate: Certificate, expires_on: date | None) -> None:
    """Задаёт или снимает срок действия сертификата."""
    certificate.expires_on = expires_on
    db.commit()


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
    full_name: str,
    login: str,
    password: str,
    role: Role,
    restaurant: str,
) -> tuple[User, str]:
    """Заводит управляющего или ещё одного сотрудника HR."""
    if role == Role.employee:
        raise BonusError("Сотрудник появляется вместе с сертификатом")
    cleaned_login = _normalize_login(login)
    if db.scalar(select(User.id).where(User.login == cleaned_login)) is not None:
        raise BonusError("Такой логин уже есть")
    plain = password.strip() or generate_password()
    if len(plain) < 8:
        raise BonusError("Пароль — минимум 8 символов")
    user = User(
        full_name=_clean_name(full_name),
        login=cleaned_login,
        password_hash=hash_password(plain),
        role=role,
        restaurant=restaurant.strip()[:200],
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, plain


def set_user_active(db: Session, user: User, active: bool) -> None:
    """Отключает доступ, не стирая журнал. Последнего HR отключить нельзя."""
    if user.role == Role.hr and not active:
        others = db.scalar(
            select(User.id).where(User.role == Role.hr, User.is_active.is_(True), User.id != user.id)
        )
        if others is None:
            raise BonusError("Нельзя отключить последнего сотрудника HR")
    user.is_active = active
    db.commit()


def find_certificate(db: Session, raw: str) -> Certificate | None:
    """Ищет сертификат по ссылке из QR, короткому коду или номеру MR-00042."""
    extracted = _extract_lookup(raw)
    if extracted == "":
        return None
    by_token = db.scalar(select(Certificate).where(Certificate.token == extracted))
    if by_token is not None:
        return by_token
    compact = extracted.replace("-", "").replace(" ", "").upper()
    by_short = db.scalar(select(Certificate).where(Certificate.short_code == compact))
    if by_short is not None:
        return by_short
    public = _public_number(compact)
    if public == "":
        return None
    return db.scalar(select(Certificate).where(Certificate.public_number == public))


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
    return existing, None


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


def _ensure_can_debit(certificate: Certificate, amount: int) -> None:
    if amount <= 0:
        raise BonusError("Сумма списания должна быть больше нуля")
    if certificate.status != CertificateStatus.active:
        raise BonusError("Сертификат заблокирован")
    if certificate.expires_on is not None and certificate.expires_on < today_msk():
        raise BonusError("Срок сертификата истёк")
    if certificate.balance < amount:
        raise BonusError("На сертификате меньше бонусов, чем вы списываете")


def _ensure_can_undo(db: Session, tx: Transaction, actor: User) -> None:
    if _is_reversed(db, tx.id):
        raise BonusError("Это списание уже отменено")
    if actor.role == Role.hr:
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
) -> Transaction:
    tx = Transaction(
        certificate_id=certificate_id,
        actor_id=actor_id,
        tx_type=tx_type,
        amount=amount,
        balance_after=balance_after,
        comment=comment,
        reversal_of_id=reversal_of_id,
    )
    db.add(tx)
    db.flush()
    return tx


def _new_token(db: Session) -> str:
    import secrets

    for _ in range(8):
        token = secrets.token_urlsafe(24)
        if db.scalar(select(Certificate.id).where(Certificate.token == token)) is None:
            return token
    raise BonusError("Не удалось выдать код сертификата")


def _new_short_code(db: Session) -> str:
    import secrets

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(8):
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if db.scalar(select(Certificate.id).where(Certificate.short_code == code)) is None:
            return code
    raise BonusError("Не удалось выдать короткий код")


def _temporary_number() -> str:
    import secrets

    return f"TMP-{secrets.token_hex(4)}"


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

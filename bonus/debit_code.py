"""Подтверждение списания бонусов одноразовым кодом."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bonus.app_settings import get_app_settings
from bonus.config import get_settings
from bonus.models import Certificate, DebitChallenge, DebitChallengeStatus, User
from bonus.push_provider import PushResult, get_push_provider
from bonus.services import BonusError, IikoDebitResult, debit_for_iiko

RESEND_COOLDOWN_SEC = 30
MAX_SENDS_PER_CHALLENGE = 3
MAX_NEW_CHALLENGES_PER_10_MIN = 5
MAX_ACTIVE_CHALLENGES = 3


@dataclass(frozen=True)
class CodeRequiredResponse:
    """Ответ 202: нужен код подтверждения."""

    challenge_id: str
    digits: int
    expires_in_sec: int
    attempts_left: int
    resend_after_sec: int
    message: str


class DebitCodeError(BonusError):
    """Ошибка сценария подтверждения кодом."""

    def __init__(self, message: str, *, attempts_left: int | None = None) -> None:
        super().__init__(message)
        self.attempts_left = attempts_left


def plugin_supports_debit_code(features_header: str) -> bool:
    """Проверяет заголовок X-Plugin-Features."""
    parts = [p.strip().lower() for p in (features_header or "").split(",") if p.strip()]
    return "debit-code" in parts


def should_require_debit_code(db: Session, certificate: Certificate, spend_amount: int) -> bool:
    """Нужен ли код для списания с учётом настроек и карты."""
    settings = get_app_settings(db)
    if not settings.debit_code_enabled:
        return False
    scope = (settings.debit_code_scope or "marked").strip().lower()
    if scope == "marked" and not certificate.require_debit_code:
        return False
    threshold = max(0, int(settings.debit_code_min_amount or 0))
    return spend_amount >= threshold


def spend_amount_for_code(db: Session, certificate: Certificate, order_amount: int) -> int:
    """Сумма списания для проверки порога: min(запрошено, доступный баланс)."""
    from bonus.parts import available_balance, ensure_legacy_part, list_parts, spend_order

    parts = list_parts(db, certificate.id)
    if not parts:
        ensure_legacy_part(db, certificate)
        parts = list_parts(db, certificate.id)
    spendable = (
        available_balance(parts, enforce_weekdays=False) if parts else certificate.balance
    )
    return min(max(0, order_amount), max(0, spendable))


def handle_iiko_debit(
    db: Session,
    certificate: Certificate,
    *,
    order_amount: int,
    external_id: str,
    restaurant: str,
    comment: str,
    order_ref: str,
    cheque_detail: str,
    challenge_id: str | None,
    code: str | None,
    plugin_features: str,
) -> IikoDebitResult | CodeRequiredResponse:
    """Маршрутизирует debit с учётом кода подтверждения."""
    _expire_stale_challenges(db)
    spend = spend_amount_for_code(db, certificate, order_amount)
    if not should_require_debit_code(db, certificate, spend):
        return debit_for_iiko(
            db,
            certificate,
            amount=order_amount,
            external_id=external_id,
            restaurant=restaurant,
            comment=comment,
            order_ref=order_ref,
            cheque_detail=cheque_detail,
        )

    if challenge_id and code:
        return _confirm_challenge(
            db,
            certificate,
            challenge_id=challenge_id.strip(),
            code=code.strip(),
            order_amount=order_amount,
            external_id=external_id,
            restaurant=restaurant,
            comment=comment,
            order_ref=order_ref,
            cheque_detail=cheque_detail,
        )

    if not plugin_supports_debit_code(plugin_features):
        raise DebitCodeError(
            "Для списания нужен код подтверждения. Обновите плагин Teams Bonus на кассе."
        )

    return _start_challenge(
        db,
        certificate,
        spend_amount=spend,
        order_amount=order_amount,
        external_id=external_id,
        restaurant=restaurant,
        comment=comment,
        order_ref=order_ref,
        cheque_detail=cheque_detail,
    )


def resend_challenge_code(db: Session, challenge_id: str) -> CodeRequiredResponse:
    """Повторная отправка push для активного вызова."""
    _expire_stale_challenges(db)
    row = db.get(DebitChallenge, challenge_id)
    if row is None or row.status != DebitChallengeStatus.pending.value:
        raise DebitCodeError("Код истёк, начните списание заново")
    now = datetime.now(timezone.utc)
    if row.last_sent_at:
        delta = (now - _aware(row.last_sent_at)).total_seconds()
        if delta < RESEND_COOLDOWN_SEC:
            raise DebitCodeError(
                f"Подождите {int(RESEND_COOLDOWN_SEC - delta)} с перед повторной отправкой"
            )
    if row.sends_count >= MAX_SENDS_PER_CHALLENGE:
        raise DebitCodeError("Лимит повторных отправок исчерпан")
    certificate = db.get(Certificate, row.certificate_id)
    if certificate is None:
        raise DebitCodeError("Карта не найдена")
    code = f"{secrets.randbelow(10000):04d}"
    row.code_hash = _hash_code(row.id, code)
    _send_push_for_challenge(db, row, certificate, code=code, resend=True)
    db.commit()
    db.refresh(row)
    return _code_required_response(row)


def cancel_challenge(db: Session, challenge_id: str) -> None:
    """Отмена вызова кассиром."""
    row = db.get(DebitChallenge, challenge_id)
    if row is None:
        return
    if row.status == DebitChallengeStatus.pending.value:
        row.status = DebitChallengeStatus.cancelled.value
        db.commit()


def _start_challenge(
    db: Session,
    certificate: Certificate,
    *,
    spend_amount: int,
    order_amount: int,
    external_id: str,
    restaurant: str,
    comment: str,
    order_ref: str,
    cheque_detail: str,
) -> CodeRequiredResponse:
    """Создаёт или переиспользует активный вызов и отправляет push."""
    settings = get_app_settings(db)
    now = datetime.now(timezone.utc)
    _enforce_rate_limits(db, certificate.id, now)
    existing = db.scalar(
        select(DebitChallenge).where(
            DebitChallenge.certificate_id == certificate.id,
            DebitChallenge.transaction_id == external_id,
            DebitChallenge.amount == order_amount,
            DebitChallenge.status == DebitChallengeStatus.pending.value,
            DebitChallenge.expires_at > now,
        )
    )
    if existing is not None:
        return _code_required_response(existing)

    challenge_id = str(uuid.uuid4())
    code = f"{secrets.randbelow(10000):04d}"
    ttl = max(30, int(settings.debit_code_ttl_sec or 120))
    max_attempts = max(1, int(settings.debit_code_max_attempts or 3))
    row = DebitChallenge(
        id=challenge_id,
        certificate_id=certificate.id,
        transaction_id=external_id,
        amount=order_amount,
        restaurant=restaurant.strip()[:200],
        code_hash=_hash_code(challenge_id, code),
        status=DebitChallengeStatus.pending.value,
        max_attempts=max_attempts,
        expires_at=now + timedelta(seconds=ttl),
    )
    db.add(row)
    db.flush()
    push_ok = _send_push_for_challenge(db, row, certificate, code=code, resend=False)
    if not push_ok.ok:
        on_failure = (settings.debit_code_on_failure or "deny").strip().lower()
        if on_failure == "allow":
            row.status = DebitChallengeStatus.cancelled.value
            db.commit()
            note = f"{(comment or 'Teams Bonus').strip()} · без кода: {push_ok.detail}"[:300]
            return debit_for_iiko(
                db,
                certificate,
                amount=order_amount,
                external_id=external_id,
                restaurant=restaurant,
                comment=note,
                order_ref=order_ref,
                cheque_detail=cheque_detail,
            )
        row.status = DebitChallengeStatus.failed.value
        db.commit()
        raise DebitCodeError(_failure_message(push_ok.detail))
    db.commit()
    db.refresh(row)
    return _code_required_response(row)


def _confirm_challenge(
    db: Session,
    certificate: Certificate,
    *,
    challenge_id: str,
    code: str,
    order_amount: int,
    external_id: str,
    restaurant: str,
    comment: str,
    order_ref: str,
    cheque_detail: str,
) -> IikoDebitResult:
    """Проверяет код и выполняет списание."""
    row = db.get(DebitChallenge, challenge_id)
    if row is None:
        raise DebitCodeError("Код истёк, начните списание заново")
    now = datetime.now(timezone.utc)
    if row.status != DebitChallengeStatus.pending.value:
        raise DebitCodeError("Код истёк, начните списание заново")
    if _aware(row.expires_at) <= now:
        row.status = DebitChallengeStatus.expired.value
        db.commit()
        raise DebitCodeError("Код истёк, начните списание заново")
    if row.certificate_id != certificate.id or row.transaction_id != external_id:
        raise DebitCodeError("Запрос не совпадает с вызовом, начните списание заново")
    if row.amount != order_amount:
        raise DebitCodeError("Сумма изменилась, начните списание заново")

    if not _verify_code(challenge_id, code, row.code_hash):
        row.attempts_used += 1
        left = max(0, row.max_attempts - row.attempts_used)
        if left <= 0:
            row.status = DebitChallengeStatus.failed.value
            db.commit()
            raise DebitCodeError("Код истёк, начните списание заново")
        db.commit()
        raise DebitCodeError(f"Неверный код, осталось попыток: {left}", attempts_left=left)

    row.status = DebitChallengeStatus.confirmed.value
    row.confirmed_at = now
    db.flush()
    note = (comment or "Teams Bonus").strip()
    note = f"{note} · подтверждено кодом"[:300]
    result = debit_for_iiko(
        db,
        certificate,
        amount=order_amount,
        external_id=external_id,
        restaurant=restaurant,
        comment=note,
        order_ref=order_ref,
        cheque_detail=cheque_detail,
    )
    db.commit()
    return result


def _send_push_for_challenge(
    db: Session,
    row: DebitChallenge,
    certificate: Certificate,
    *,
    code: str | None = None,
    resend: bool,
) -> PushResult:
    """Отправляет push держателю карты."""
    holder = db.get(User, certificate.holder_id)
    spasibo_id = (holder.spasibo_user_id if holder else "") or ""
    settings = get_app_settings(db)
    if not spasibo_id.strip():
        on_failure = (settings.debit_code_on_failure or "deny").strip().lower()
        if on_failure == "allow":
            return PushResult(ok=False, detail="no spasibo link")
        return PushResult(ok=False, detail="У карты нет привязки к «Спасибо»")

    if code is None:
        code = "****"
    provider = get_push_provider()
    result = provider.send_debit_code(
        spasibo_id.strip(),
        code,
        row.amount,
        row.restaurant,
    )
    row.sends_count += 1
    row.last_sent_at = datetime.now(timezone.utc)
    row.push_status = result.detail[:120]
    if not resend:
        db.flush()
    return result


def _failure_message(detail: str) -> str:
    if "привязки" in detail.lower():
        return "Нельзя отправить код: у карты нет приложения «Спасибо». Списание отклонено."
    return "Не удалось отправить код подтверждения. Списание отклонено."


def _code_required_response(row: DebitChallenge) -> CodeRequiredResponse:
    now = datetime.now(timezone.utc)
    expires_at = _aware(row.expires_at)
    expires_in = max(0, int((expires_at - now).total_seconds()))
    attempts_left = max(0, row.max_attempts - row.attempts_used)
    resend_after = RESEND_COOLDOWN_SEC
    if row.last_sent_at:
        elapsed = (now - _aware(row.last_sent_at)).total_seconds()
        resend_after = max(0, int(RESEND_COOLDOWN_SEC - elapsed))
    return CodeRequiredResponse(
        challenge_id=row.id,
        digits=4,
        expires_in_sec=expires_in,
        attempts_left=attempts_left,
        resend_after_sec=resend_after,
        message="Гостю отправлен код в приложение «Спасибо»",
    )


def _enforce_rate_limits(db: Session, certificate_id: int, now: datetime) -> None:
    window_start = now - timedelta(minutes=10)
    recent = db.scalar(
        select(func.count())
        .select_from(DebitChallenge)
        .where(
            DebitChallenge.certificate_id == certificate_id,
            DebitChallenge.created_at >= window_start,
        )
    )
    if (recent or 0) >= MAX_NEW_CHALLENGES_PER_10_MIN:
        raise DebitCodeError("Слишком много попыток, подождите")
    active = db.scalar(
        select(func.count())
        .select_from(DebitChallenge)
        .where(
            DebitChallenge.certificate_id == certificate_id,
            DebitChallenge.status == DebitChallengeStatus.pending.value,
            DebitChallenge.expires_at > now,
        )
    )
    if (active or 0) >= MAX_ACTIVE_CHALLENGES:
        raise DebitCodeError("Слишком много попыток, подождите")


def _expire_stale_challenges(db: Session) -> None:
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(DebitChallenge).where(
            DebitChallenge.status == DebitChallengeStatus.pending.value,
            DebitChallenge.expires_at <= now,
        )
    ).all()
    for row in rows:
        row.status = DebitChallengeStatus.expired.value
    if rows:
        db.commit()


def _hash_code(challenge_id: str, code: str) -> str:
    secret = get_settings().secret_key.encode("utf-8")
    msg = f"{challenge_id}:{code}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _verify_code(challenge_id: str, code: str, stored_hash: str) -> bool:
    if len(code) != 4 or not code.isdigit():
        return False
    expected = _hash_code(challenge_id, code)
    return hmac.compare_digest(expected, stored_hash or "")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

"""Ограничение подбора пароля по IP: счётчики в БД, не в памяти процесса."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import LoginAttempt
from bonus.values import MSK, as_utc

MAX_FAILS = 5
BLOCK_HOURS = 6
_PURGE_AFTER = timedelta(hours=24)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
logger = logging.getLogger("bonus.login_guard")


def login_block_status(
    db: Session,
    ip: str,
    now: datetime | None = None,
) -> tuple[bool, datetime | None]:
    """Возвращает (заблокирован ли IP, до какого момента)."""
    cleaned = _clean_ip(ip)
    if not cleaned or _ip_allowed(cleaned):
        return False, None
    moment = _as_utc(now)
    row = db.get(LoginAttempt, cleaned)
    if row is None or row.blocked_until is None:
        return False, None
    until = as_utc(row.blocked_until)
    if until <= moment:
        return False, None
    return True, until


def register_login_failure(
    db: Session,
    ip: str,
    *,
    login: str = "",
    now: datetime | None = None,
) -> LoginAttempt:
    """Увеличивает счётчик; на 5-й неудаче ставит блок на BLOCK_HOURS."""
    cleaned = _clean_ip(ip) or "unknown"
    moment = _as_utc(now)
    purge_old_attempts(db, moment)
    if _ip_allowed(cleaned):
        row = LoginAttempt(
            ip=cleaned,
            fail_count=0,
            last_fail_at=moment,
            blocked_until=None,
        )
        return row

    row = db.execute(
        select(LoginAttempt).where(LoginAttempt.ip == cleaned).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        row = LoginAttempt(
            ip=cleaned,
            fail_count=0,
            last_fail_at=moment,
            blocked_until=None,
        )
        db.add(row)
        db.flush()

    window_start = moment - timedelta(hours=BLOCK_HOURS)
    last_fail = as_utc(row.last_fail_at) if row.last_fail_at else None
    blocked_until = as_utc(row.blocked_until) if row.blocked_until else None
    if blocked_until is not None and blocked_until <= moment:
        row.blocked_until = None
        blocked_until = None
    if last_fail is None or last_fail < window_start:
        row.fail_count = 0

    row.fail_count = int(row.fail_count or 0) + 1
    row.last_fail_at = moment
    if row.fail_count >= MAX_FAILS:
        row.blocked_until = moment + timedelta(hours=BLOCK_HOURS)
    db.commit()
    db.refresh(row)
    logger.warning(
        "login fail ip=%s login=%s attempt=%s",
        cleaned,
        (login or "").strip()[:64],
        row.fail_count,
    )
    return row


def register_login_success(db: Session, ip: str) -> None:
    """Сбрасывает счётчик после успешного входа."""
    cleaned = _clean_ip(ip)
    if not cleaned:
        return
    db.execute(delete(LoginAttempt).where(LoginAttempt.ip == cleaned))
    db.commit()


def purge_old_attempts(db: Session, now: datetime | None = None) -> int:
    """Удаляет записи старше суток (и блок, и последняя неудача)."""
    moment = _as_utc(now)
    cutoff = moment - _PURGE_AFTER
    result = db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.last_fail_at < cutoff,
            or_(
                LoginAttempt.blocked_until.is_(None),
                LoginAttempt.blocked_until < cutoff,
            ),
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def unblock_login_ip(db: Session, ip: str) -> bool:
    """Снимает блок вручную. True, если запись была."""
    cleaned = _clean_ip(ip)
    if not cleaned:
        return False
    result = db.execute(delete(LoginAttempt).where(LoginAttempt.ip == cleaned))
    db.commit()
    return int(result.rowcount or 0) > 0


def list_blocked_attempts(db: Session, now: datetime | None = None) -> list[LoginAttempt]:
    """Активные блоки для таблицы в Настройках."""
    moment = _as_utc(now)
    rows = db.scalars(
        select(LoginAttempt)
        .where(
            LoginAttempt.blocked_until.is_not(None),
            LoginAttempt.blocked_until > moment,
        )
        .order_by(LoginAttempt.blocked_until.desc())
    ).all()
    return list(rows)


def attempts_left(fail_count: int) -> int:
    """Сколько попыток осталось до блока (после текущей неудачи)."""
    return max(0, MAX_FAILS - int(fail_count or 0))


def failure_message(fail_count: int) -> str:
    """Текст ошибки без раскрытия существования логина."""
    left = attempts_left(fail_count)
    if left <= 0:
        return "Неверный логин или пароль. Это последняя попытка"
    if left == 1:
        return "Неверный логин или пароль. Это последняя попытка"
    return f"Неверный логин или пароль. Осталось попыток: {left}"


def block_message(blocked_until: datetime, support_handle: str = "") -> str:
    """Сообщение 429: до какого времени закрыт вход (МСК)."""
    local = as_utc(blocked_until).astimezone(MSK)
    stamp = local.strftime("%H:%M")
    text = (
        f"Слишком много неудачных попыток. Вход с этого адреса закрыт до "
        f"{stamp} (МСК)."
    )
    handle = normalize_support_telegram(support_handle)
    if handle:
        text += f" Если вы забыли пароль, напишите в Telegram {handle}."
    return text


def normalize_support_telegram(raw: str) -> str:
    """Нормализует @handle; недопустимое значение → пустая строка."""
    text = (raw or "").strip()
    if text.startswith("@"):
        text = text[1:]
    text = text.strip()
    if not _HANDLE_RE.fullmatch(text):
        return ""
    return f"@{text}"


def support_telegram_url(handle: str) -> str:
    """Ссылка t.me для нормализованного @handle."""
    cleaned = normalize_support_telegram(handle)
    if not cleaned:
        return ""
    return f"https://t.me/{cleaned[1:]}"


def _ip_allowed(ip: str) -> bool:
    allowed = {
        item.strip()
        for item in (get_settings().login_allow_ips or "").split(",")
        if item.strip()
    }
    return ip in allowed


def _clean_ip(ip: str) -> str:
    return (ip or "").strip()[:64]


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return as_utc(value)

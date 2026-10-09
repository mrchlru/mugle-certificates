"""Пароли, сессия и ограничение попыток входа."""

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

from fastapi import Request
from sqlalchemy.orm import Session

from bonus.models import Role, User

_ATTEMPTS: dict[str, list[float]] = {}
_WINDOW_SECONDS = 600
_MAX_ATTEMPTS = 8
_DUMMY_HASH = ""


def hash_password(password: str) -> str:
    """Хеширует пароль через PBKDF2. В базе нет открытого пароля."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return base64.b64encode(salt + digest).decode()


def verify_password(password: str, stored: str) -> bool:
    """Сверяет пароль с хешем, не раскрывая, на каком байте они разошлись."""
    try:
        raw = base64.b64decode(stored.encode())
    except Exception:
        return False
    salt, digest = raw[:16], raw[16:]
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(digest, check)


def generate_password() -> str:
    """Короткий пароль, который HR может продиктовать сотруднику."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def authenticate(db: Session, login: str, password: str) -> User | None:
    """Возвращает активного пользователя или None, если пара не подошла."""
    user = db.query(User).filter(User.login == login.strip().casefold()).one_or_none()
    stored = user.password_hash if user is not None else _dummy_hash()
    matched = verify_password(password, stored)
    if user is None or not matched or not user.is_active:
        return None
    return user


def login_blocked(ip: str) -> bool:
    """Останавливает подбор пароля с одного адреса."""
    now = time.monotonic()
    hits = [moment for moment in _ATTEMPTS.get(ip, []) if now - moment < _WINDOW_SECONDS]
    _ATTEMPTS[ip] = hits
    return len(hits) >= _MAX_ATTEMPTS


def login_failed(ip: str) -> None:
    """Запоминает неудачную попытку входа."""
    _ATTEMPTS.setdefault(ip, []).append(time.monotonic())


def login_succeeded(ip: str) -> None:
    """Сбрасывает счётчик после верного пароля."""
    _ATTEMPTS.pop(ip, None)


def client_ip(request: Request) -> str:
    """Берёт адрес клиента. За прокси Timeweb смотрит на X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is None:
        return "local"
    return request.client.host


def ensure_csrf(request: Request) -> str:
    """Кладёт в сессию токен формы и возвращает его."""
    token = request.session.get("csrf")
    if not isinstance(token, str) or token == "":
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def csrf_ok(request: Request, token: str) -> bool:
    """Проверяет, что форма отправлена с той же сессии."""
    expected = request.session.get("csrf", "")
    if not isinstance(expected, str) or expected == "" or token == "":
        return False
    return hmac.compare_digest(token, expected)


def flash(request: Request, message: str, level: str = "ok") -> None:
    """Кладёт сообщение, которое покажется после перехода."""
    bag = list(request.session.get("flashes", []))
    bag.append({"message": message, "level": level})
    request.session["flashes"] = bag


def pop_flashes(request: Request) -> list[dict[str, str]]:
    """Забирает сообщения, чтобы они не повторялись на следующей странице."""
    flashes = list(request.session.get("flashes", []))
    if flashes:
        request.session["flashes"] = []
    return flashes


def home_for(role: Role) -> str:
    """Куда попадает человек сразу после входа."""
    if role == Role.hr:
        return "/hr"
    if role == Role.manager:
        return "/desk"
    return "/card"


def safe_next(value: str, fallback: str = "/") -> str:
    """Оставляет только внутренний адрес, без увода на чужой сайт."""
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


def login_redirect(next_path: str) -> str:
    """Адрес страницы входа с возвратом назад."""
    return f"/login?next={quote(safe_next(next_path), safe='')}"


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH == "":
        _DUMMY_HASH = hash_password("dummy-password-for-timing")
    return _DUMMY_HASH

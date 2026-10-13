"""Общие куски страниц: отрисовка, вход и проверка формы."""

from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from bonus import __version__
from bonus.catalog import accrual_label
from bonus.config import get_settings
from bonus.db import get_db, open_session
from bonus.models import Role, User
from bonus.security import csrf_ok, ensure_csrf, home_for, login_redirect, pop_flashes
from bonus.services import BonusError
from bonus.values import (
    MoneyError,
    format_date,
    format_dt,
    format_money,
    format_money_plain,
    format_short_code,
    parse_money,
    status_label,
    tx_label,
)

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
templates.env.filters["money"] = format_money
templates.env.filters["money_plain"] = format_money_plain
templates.env.filters["when"] = format_dt
templates.env.filters["day"] = format_date
templates.env.globals["status_label"] = status_label
templates.env.globals["tx_label"] = tx_label
templates.env.globals["format_short_code"] = format_short_code
templates.env.globals["format_date"] = format_date
templates.env.globals["accrual_label"] = accrual_label


class Redirect(Exception):
    """Маршрут просит браузер перейти на другую страницу."""

    def __init__(self, url: str, status_code: int = 302) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(url)


@dataclass(frozen=True)
class Account:
    """Пользователь в шапке. Это уже не объект базы."""

    id: int
    full_name: str
    login: str
    role: Role
    restaurant: str
    must_change_password: bool


def render(request: Request, name: str, status_code: int = 200, **context: object):
    """Собирает страницу и запрещает браузеру кэшировать остаток бонусов."""
    context["user"] = account_from_request(request)
    context["flashes"] = pop_flashes(request)
    context["csrf"] = ensure_csrf(request)
    context["warn_secret"] = get_settings().uses_default_secret()
    context["asset_v"] = __version__
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def account_from_request(request: Request) -> Account | None:
    """Читает, кто вошёл, и сразу отсоединяет запись от базы."""
    raw = request.session.get("user_id")
    if raw is None:
        return None
    db = open_session()
    try:
        user = db.get(User, int(raw))
        if user is None or not user.is_active:
            return None
        return account_from_user(user)
    finally:
        db.close()


def account_from_user(user: User) -> Account:
    """Копирует поля пользователя, пока сессия базы ещё открыта."""
    return Account(
        id=user.id,
        full_name=user.full_name,
        login=user.login,
        role=user.role,
        restaurant=user.restaurant,
        must_change_password=user.must_change_password,
    )


def require(*roles: Role):
    """Пускает только вошедшего человека нужной роли."""

    def dependency(request: Request, db: Session = Depends(get_db)) -> User:
        user = load_user(request, db)
        if user is None:
            raise Redirect(login_redirect(request.url.path))
        if roles and user.role not in roles:
            raise Redirect(home_for(user.role))
        return user

    return dependency


def load_user(request: Request, db: Session) -> User | None:
    """Достаёт активного пользователя внутри уже открытой сессии."""
    raw = request.session.get("user_id")
    if raw is None:
        return None
    user = db.get(User, int(raw))
    if user is None or not user.is_active:
        return None
    return user


def guard_csrf(request: Request, token: str) -> None:
    """Останавливает форму, если токен сессии не совпал."""
    if not csrf_ok(request, token):
        raise BonusError("Сессия устарела. Обновите страницу и повторите.")


def form_money(raw: str) -> int:
    """Сумма из поля формы в копейках. Ноль и пустое поле не подходят."""
    try:
        value = parse_money(raw)
    except MoneyError as exc:
        raise BonusError(exc.message) from exc
    if value is None or value <= 0:
        raise BonusError("Сумма должна быть больше нуля")
    return value


def optional_money(raw: str) -> int | None:
    """Пустое поле суммы остаётся пустым, а не нулём."""
    try:
        return parse_money(raw, blank_as_none=True)
    except MoneyError as exc:
        raise BonusError(exc.message) from exc


def public_base(request: Request) -> str:
    """Адрес, который попадёт в QR. На Timeweb лучше задать APP_PUBLIC_URL."""
    configured = get_settings().app_public_url.strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")

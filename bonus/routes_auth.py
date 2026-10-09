"""Вход, выход и смена своего пароля."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.db import get_db
from bonus.http import guard_csrf, load_user, render
from bonus.http import Redirect as PageRedirect
from bonus.models import Role, User
from bonus.security import (
    authenticate,
    client_ip,
    flash,
    home_for,
    login_blocked,
    login_failed,
    login_succeeded,
    safe_next,
)
from bonus.services import BonusError, change_password

router = APIRouter()


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    """Отправляет человека в его раздел или на вход."""
    user = load_user(request, db)
    if user is None:
        raise PageRedirect("/login")
    raise PageRedirect(home_for(user.role))


def _current(request: Request, db: Session = Depends(get_db)) -> User:
    user = load_user(request, db)
    if user is None:
        raise PageRedirect("/login?next=/account")
    return user


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db), next: str = "/"):
    """Страница входа. Уже вошедшего отправляет в его раздел."""
    user = load_user(request, db)
    if user is not None:
        raise PageRedirect(home_for(user.role))
    return render(request, "login.html", next_path=safe_next(next))


@router.post("/login")
def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    login: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    next: str = Form("/"),
):
    """Проверяет логин и открывает карточку, кассу или кабинет HR."""
    target = safe_next(next)
    try:
        guard_csrf(request, csrf_token)
        ip = client_ip(request)
        if login_blocked(ip):
            raise BonusError("Слишком много попыток. Подождите десять минут.")
        holder = db.scalar(select(User).where(User.login == login.strip().casefold()))
        if holder is not None and holder.role == Role.employee:
            raise BonusError("Сертификат открывается по ссылке. Её отправляет HR, пароль не нужен.")
        user = authenticate(db, login, password)
        if user is None:
            login_failed(ip)
            raise BonusError("Неверный логин или пароль")
        login_succeeded(ip)
    except BonusError as exc:
        return render(request, "login.html", status_code=400, next_path=target, error=exc.message, login=login)
    request.session["user_id"] = user.id
    destination = target if target not in {"", "/"} else home_for(user.role)
    return RedirectResponse(destination, status_code=303)


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    """Забывает сессию."""
    if csrf_ok_or_clear(request, csrf_token):
        request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/account")
def account_form(request: Request, user: User = Depends(_current)):
    """Форма смены своего пароля."""
    return render(request, "account.html")


@router.post("/account")
def account_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_current),
    current_password: str = Form(""),
    new_password: str = Form(""),
    csrf_token: str = Form(""),
):
    """Сохраняет новый пароль."""
    try:
        guard_csrf(request, csrf_token)
        change_password(db, user, current_password, new_password)
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse("/account", status_code=303)
    flash(request, "Пароль изменён")
    return RedirectResponse(home_for(user.role), status_code=303)


def csrf_ok_or_clear(request: Request, token: str) -> bool:
    """Выход без верного токена просто не чистит сессию."""
    from bonus.security import csrf_ok

    return csrf_ok(request, token)

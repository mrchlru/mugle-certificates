"""Кабинет HR: сводка, выпуск, импорт и журнал."""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from bonus.db import get_db
from bonus.http import form_money, guard_csrf, optional_money, public_base, render, require
from bonus.importer import ImportRowResult, parse_table
from bonus.models import Role, User
from bonus.present import dashboard_stats, ledger, list_staff, load_card, search_certificates
from bonus.security import flash
from bonus.services import (
    BonusError,
    IssueDraft,
    create_staff,
    credit_certificate,
    issue_certificate,
    rename_holder,
    set_blocked,
    set_user_active,
)
from bonus.values import MoneyError, format_money, parse_date

router = APIRouter()


@router.get("/hr")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    q: str = "",
):
    """Сводка и поиск сертификатов."""
    return render(
        request,
        "hr.html",
        section="home",
        stats=dashboard_stats(db),
        cards=search_certificates(db, q),
        query=q,
    )


@router.get("/hr/new")
def issue_form(request: Request, user: User = Depends(require(Role.hr))):
    """Форма нового сертификата."""
    return render(request, "hr_issue.html", section="issue")


@router.post("/hr/new")
def issue_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    full_name: str = Form(""),
    login: str = Form(""),
    nominal: str = Form(""),
    note: str = Form(""),
    expires_on: str = Form(""),
    csrf_token: str = Form(""),
):
    """Выписывает сертификат на полный номинал и ведёт к ссылке для сотрудника."""
    try:
        guard_csrf(request, csrf_token)
        nominal_value = optional_money(nominal)
        if nominal_value is None:
            raise BonusError("Укажите номинал")
        issued = issue_certificate(
            db,
            IssueDraft(
                full_name=full_name,
                login=login,
                password="",
                nominal=nominal_value,
                balance=None,
                note=note,
                expires_on=_expires(expires_on),
            ),
            user,
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse("/hr/new", status_code=303)
    flash(request, f"Сертификат {issued.certificate.public_number} выписан. Отправьте ссылку сотруднику.")
    return RedirectResponse(f"/hr/c/{issued.certificate.id}", status_code=303)


@router.get("/hr/c/{certificate_id}")
def certificate_page(
    certificate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
):
    """Карточка, история и действия HR."""
    certificate = _certificate_or_redirect(db, request, certificate_id)
    if certificate is None:
        return RedirectResponse("/hr", status_code=303)
    card = load_card(db, certificate, public_base(request), user)
    share_url = f"{public_base(request)}/c/{certificate.token}"
    return render(request, "hr_certificate.html", section="home", card=card, share_url=share_url)


@router.post("/hr/c/{certificate_id}/credit")
def credit_submit(
    certificate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    amount: str = Form(""),
    comment: str = Form(""),
    csrf_token: str = Form(""),
):
    """Добавляет бонусы к остатку сертификата."""
    certificate = _certificate_or_redirect(db, request, certificate_id)
    if certificate is None:
        return RedirectResponse("/hr", status_code=303)
    try:
        guard_csrf(request, csrf_token)
        added = form_money(amount)
        credit_certificate(db, certificate, user, added, comment)
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, f"Начислено {format_money(added)}")
    return RedirectResponse(f"/hr/c/{certificate_id}", status_code=303)


@router.post("/hr/c/{certificate_id}/block")
def block_submit(
    certificate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    blocked: str = Form("1"),
    csrf_token: str = Form(""),
):
    """Блокирует или возвращает сертификат в работу."""
    certificate = _certificate_or_redirect(db, request, certificate_id)
    if certificate is None:
        return RedirectResponse("/hr", status_code=303)
    try:
        guard_csrf(request, csrf_token)
        set_blocked(db, certificate, user, blocked == "1")
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Блокировка снята" if blocked != "1" else "Сертификат заблокирован")
    return RedirectResponse(f"/hr/c/{certificate_id}", status_code=303)


@router.post("/hr/c/{certificate_id}/name")
def name_submit(
    certificate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    full_name: str = Form(""),
    csrf_token: str = Form(""),
):
    """Исправляет имя на карточке."""
    certificate = _certificate_or_redirect(db, request, certificate_id)
    if certificate is None:
        return RedirectResponse("/hr", status_code=303)
    holder = db.get(User, certificate.holder_id)
    try:
        guard_csrf(request, csrf_token)
        if holder is None:
            raise BonusError("Сотрудник не найден")
        rename_holder(db, holder, full_name)
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Имя обновлено")
    return RedirectResponse(f"/hr/c/{certificate_id}", status_code=303)


@router.get("/hr/ledger")
def ledger_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require(Role.hr))):
    """Журнал списаний и остальных операций."""
    return render(request, "hr_ledger.html", section="ledger", rows=ledger(db, user))


@router.get("/hr/import")
def import_form(request: Request, user: User = Depends(require(Role.hr))):
    """Загрузка таблицы из старого Statix Bonus."""
    return render(request, "hr_import.html", section="import", results=None)


@router.post("/hr/import")
def import_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    table: UploadFile = File(...),
    csrf_token: str = Form(""),
):
    """Переносит строки таблицы в сертификаты."""
    try:
        guard_csrf(request, csrf_token)
        payload = table.file.read()
        parsed = parse_table(table.filename or "table.csv", payload)
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse("/hr/import", status_code=303)
    results = [_import_row(db, user, row, public_base(request)) for row in parsed]
    return render(request, "hr_import.html", section="import", results=results)


@router.get("/hr/import/sample.csv")
def sample_csv(user: User = Depends(require(Role.hr))) -> Response:
    """Пример таблицы, который открывается в Excel."""
    body = (
        "ФИО;Логин;Номинал;Комментарий;Срок\n"
        "Иванов Иван;ivanov;10000;Офис;31.12.2026\n"
        "Петрова Анна;petrova;5000;Кухня;\n"
    )
    return Response(
        content=body.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sertifikaty.csv"},
    )


@router.get("/hr/staff")
def staff_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require(Role.hr))):
    """Управляющие и сотрудники HR."""
    return render(request, "hr_staff.html", section="staff", staff=list_staff(db))


@router.post("/hr/staff")
def staff_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    full_name: str = Form(""),
    login: str = Form(""),
    password: str = Form(""),
    role: str = Form("manager"),
    restaurant: str = Form(""),
    csrf_token: str = Form(""),
):
    """Создаёт вход для управляющего или HR."""
    try:
        guard_csrf(request, csrf_token)
        created, plain = create_staff(
            db,
            full_name=full_name,
            login=login,
            password=password,
            role=Role.hr if role == "hr" else Role.manager,
            restaurant=restaurant,
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse("/hr/staff", status_code=303)
    flash(request, f"Доступ для {created.full_name} создан")
    flash(request, plain, "secret")
    return RedirectResponse("/hr/staff", status_code=303)


@router.post("/hr/staff/{user_id}/active")
def staff_active(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require(Role.hr)),
    active: str = Form("0"),
    csrf_token: str = Form(""),
):
    """Включает или отключает вход."""
    person = db.get(User, user_id)
    try:
        guard_csrf(request, csrf_token)
        if person is None or person.role == Role.employee:
            raise BonusError("Пользователь не найден")
        set_user_active(db, person, active == "1")
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Доступ обновлён")
    return RedirectResponse("/hr/staff", status_code=303)


def _certificate_or_redirect(db: Session, request: Request, certificate_id: int):
    from bonus.models import Certificate

    found = db.get(Certificate, certificate_id)
    if found is None:
        flash(request, "Сертификат не найден", "bad")
        return None
    return found


def _expires(raw: str):
    try:
        return parse_date(raw)
    except MoneyError as exc:
        raise BonusError(exc.message) from exc


def _import_row(db: Session, actor: User, row, base_url: str) -> ImportRowResult:
    if row.draft is None:
        return ImportRowResult(row_number=row.row_number, ok=False, message=row.error)
    try:
        issued = issue_certificate(db, row.draft, actor)
    except BonusError as exc:
        db.rollback()
        return ImportRowResult(row_number=row.row_number, ok=False, message=exc.message)
    return ImportRowResult(
        row_number=row.row_number,
        ok=True,
        message="Выписан",
        login=issued.user.login,
        public_number=issued.certificate.public_number,
        link=f"{base_url}/c/{issued.certificate.token}",
    )

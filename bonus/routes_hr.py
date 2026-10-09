"""Кабинет HR: сводка, выпуск, импорт и журнал."""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from bonus.catalog import (
    ACCRUAL_CHOICES,
    CARD_COLORS,
    create_segment,
    list_segments,
    list_templates,
    media_url,
    normalize_color,
    parse_accrual_period,
    resolve_segment,
    resolve_template,
    save_template,
    set_segment_active,
    set_template_active,
    update_segment,
    update_template,
)
from bonus.db import get_db
from bonus.http import form_money, guard_csrf, optional_money, public_base, render, require
from bonus.importer import ImportRowResult, parse_table
from bonus.models import CardTemplate, Role, Segment, User
from bonus.present import dashboard_stats, ledger, list_staff, load_card, search_certificates
from bonus.security import flash
from bonus.services import (
    BonusError,
    IssueDraft,
    create_staff,
    credit_certificate,
    issue_certificate,
    set_blocked,
    set_certificate_expiry,
    set_user_active,
    update_holder_profile,
)
from bonus.values import MoneyError, format_money, parse_date

router = APIRouter()


@router.get("/hr")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    q: str = "",
    segment: int = 0,
):
    """Сводка и поиск сертификатов с фильтром по сегменту."""
    segment_id = segment if segment > 0 else None
    return render(
        request,
        "hr.html",
        section="home",
        stats=dashboard_stats(db),
        cards=search_certificates(db, q, segment_id),
        query=q,
        segment_id=segment_id or 0,
        segments=list_segments(db),
    )


@router.get("/hr/new")
def issue_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require(Role.hr))):
    """Форма нового сертификата."""
    return render(
        request,
        "hr_issue.html",
        section="issue",
        segments=list_segments(db),
        templates=list_templates(db),
        colors=CARD_COLORS,
    )


@router.post("/hr/new")
def issue_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    full_name: str = Form(""),
    department: str = Form(""),
    job_title: str = Form(""),
    phone: str = Form(""),
    nominal: str = Form(""),
    note: str = Form(""),
    expires_on: str = Form(""),
    segment_id: int = Form(0),
    template_id: int = Form(0),
    color: str = Form("#00A8A8"),
    csrf_token: str = Form(""),
):
    """Выписывает сертификат на полный номинал и ведёт к ссылке для сотрудника."""
    try:
        guard_csrf(request, csrf_token)
        nominal_value = optional_money(nominal)
        if nominal_value is None:
            raise BonusError("Укажите номинал")
        segment = resolve_segment(db, segment_id)
        template = resolve_template(db, template_id)
        expires = _expires(expires_on)
        if segment.requires_expiry and expires is None:
            raise BonusError("Укажите срок действия для этого сегмента")
        issued = issue_certificate(
            db,
            IssueDraft(
                full_name=full_name,
                login="",
                password="",
                nominal=nominal_value,
                balance=None,
                note=note,
                expires_on=expires,
                segment_id=segment.id,
                template_id=None if template is None else template.id,
                color=normalize_color(color),
                department=department,
                job_title=job_title,
                phone=phone,
            ),
            user,
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse("/hr/new", status_code=303)
    flash(request, f"Сертификат {issued.certificate.public_number} выписан. Отправьте ссылку сотруднику.")
    return RedirectResponse(f"/hr/c/{issued.certificate.id}", status_code=303)


@router.get("/hr/design")
def design_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require(Role.hr))):
    """Сегменты и шаблоны для внешнего вида карточек."""
    return render(
        request,
        "hr_design.html",
        section="design",
        segments=list_segments(db, active_only=False),
        templates=list_templates(db, active_only=False),
        media_url=media_url,
        accrual_choices=ACCRUAL_CHOICES,
    )


@router.post("/hr/design/segments")
def segment_create(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    name: str = Form(""),
    accrual_period: str = Form("none"),
    requires_expiry: str = Form("0"),
    csrf_token: str = Form(""),
):
    """Добавляет свой сегмент с периодичностью начисления."""
    try:
        guard_csrf(request, csrf_token)
        create_segment(
            db,
            name,
            parse_accrual_period(accrual_period),
            requires_expiry=requires_expiry == "1",
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Сегмент добавлен")
    return RedirectResponse("/hr/design", status_code=303)


@router.post("/hr/design/segments/{segment_id}")
def segment_update(
    segment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    name: str = Form(""),
    accrual_period: str = Form("none"),
    requires_expiry: str = Form("0"),
    csrf_token: str = Form(""),
):
    """Редактирует название, периодичность и срок действия сегмента."""
    segment = db.get(Segment, segment_id)
    try:
        guard_csrf(request, csrf_token)
        if segment is None:
            raise BonusError("Сегмент не найден")
        update_segment(
            db,
            segment,
            name=name,
            accrual_period=parse_accrual_period(accrual_period),
            requires_expiry=requires_expiry == "1",
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Сегмент сохранён")
    return RedirectResponse("/hr/design", status_code=303)


@router.post("/hr/design/segments/{segment_id}/active")
def segment_active(
    segment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    active: str = Form("0"),
    csrf_token: str = Form(""),
):
    """Включает или прячет сегмент."""
    segment = db.get(Segment, segment_id)
    try:
        guard_csrf(request, csrf_token)
        if segment is None:
            raise BonusError("Сегмент не найден")
        set_segment_active(db, segment, active == "1")
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Сегмент обновлён")
    return RedirectResponse("/hr/design", status_code=303)


@router.post("/hr/design/templates")
def template_create(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    name: str = Form(""),
    image: UploadFile = File(...),
    csrf_token: str = Form(""),
):
    """Загружает картинку шаблона для баннера карточки."""
    try:
        guard_csrf(request, csrf_token)
        payload = image.file.read()
        save_template(
            db,
            name=name,
            filename=image.filename or "template.png",
            payload=payload,
            content_type=image.content_type or "",
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Шаблон загружен")
    return RedirectResponse("/hr/design", status_code=303)


@router.post("/hr/design/templates/{template_id}")
async def template_update(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    name: str = Form(""),
    image: UploadFile | None = File(None),
    csrf_token: str = Form(""),
):
    """Меняет название шаблона и при желании заменяет картинку."""
    template = db.get(CardTemplate, template_id)
    try:
        guard_csrf(request, csrf_token)
        if template is None:
            raise BonusError("Шаблон не найден")
        payload: bytes | None = None
        content_type = ""
        filename = ""
        if image is not None and image.filename:
            payload = await image.read()
            content_type = image.content_type or ""
            filename = image.filename
        update_template(
            db,
            template,
            name=name,
            filename=filename,
            payload=payload,
            content_type=content_type,
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Шаблон сохранён")
    return RedirectResponse("/hr/design", status_code=303)


@router.post("/hr/design/templates/{template_id}/active")
def template_active(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    active: str = Form("0"),
    csrf_token: str = Form(""),
):
    """Включает или отключает шаблон."""
    template = db.get(CardTemplate, template_id)
    try:
        guard_csrf(request, csrf_token)
        if template is None:
            raise BonusError("Шаблон не найден")
        set_template_active(db, template, active == "1")
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Шаблон обновлён")
    return RedirectResponse("/hr/design", status_code=303)


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
    department: str = Form(""),
    job_title: str = Form(""),
    phone: str = Form(""),
    csrf_token: str = Form(""),
):
    """Обновляет ФИО и внутренние поля сотрудника."""
    certificate = _certificate_or_redirect(db, request, certificate_id)
    if certificate is None:
        return RedirectResponse("/hr", status_code=303)
    holder = db.get(User, certificate.holder_id)
    try:
        guard_csrf(request, csrf_token)
        if holder is None:
            raise BonusError("Сотрудник не найден")
        update_holder_profile(
            db,
            holder,
            full_name=full_name,
            department=department,
            job_title=job_title,
            phone=phone,
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Данные сотрудника сохранены")
    return RedirectResponse(f"/hr/c/{certificate_id}", status_code=303)


@router.post("/hr/c/{certificate_id}/expires")
def expires_submit(
    certificate_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    expires_on: str = Form(""),
    csrf_token: str = Form(""),
):
    """Задаёт срок действия сертификата."""
    certificate = _certificate_or_redirect(db, request, certificate_id)
    if certificate is None:
        return RedirectResponse("/hr", status_code=303)
    try:
        guard_csrf(request, csrf_token)
        expires = _expires(expires_on)
        segment = db.get(Segment, certificate.segment_id) if certificate.segment_id else None
        if segment is not None and segment.requires_expiry and expires is None:
            raise BonusError("Укажите срок действия для этого сегмента")
        set_certificate_expiry(db, certificate, expires)
    except BonusError as exc:
        flash(request, exc.message, "bad")
    else:
        flash(request, "Срок действия сохранён")
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

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
from dataclasses import replace

from sqlalchemy import select

from bonus.importer import ImportRowResult, build_sample_workbook, parse_table
from bonus.storage import s3_enabled
from bonus.models import CardTemplate, Role, Segment, User
from bonus.ledger import (
    LEDGER_CHOICES,
    ledger_is_single_use,
    ledger_needs_birth_date,
    parse_ledger_sheet,
)
from bonus.present import dashboard_stats, ledger, list_staff, load_card, search_certificates
from bonus.security import flash
from bonus.services import (
    BonusError,
    IssueDraft,
    create_staff,
    credit_certificate,
    issue_certificate,
    normalize_discount,
    set_blocked,
    set_certificate_expiry,
    set_user_active,
    update_holder_profile,
    update_staff,
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
        media_url=media_url,
        ledger_choices=LEDGER_CHOICES,
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
    company: str = Form(""),
    birth_date: str = Form(""),
    ledger_sheet: str = Form("employee"),
    nominal: str = Form(""),
    note: str = Form(""),
    expires_on: str = Form(""),
    discount_percent: str = Form(""),
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
        try:
            sheet = parse_ledger_sheet(ledger_sheet)
        except ValueError as exc:
            raise BonusError(str(exc)) from exc
        segment = resolve_segment(db, segment_id)
        template = resolve_template(db, template_id)
        expires = _expires(expires_on)
        if segment.requires_expiry and expires is None:
            raise BonusError("Укажите срок действия для этого сегмента")
        born = _expires(birth_date)
        if ledger_needs_birth_date(sheet) and born is None:
            raise BonusError("Укажите дату рождения")
        if not company.strip():
            raise BonusError("Укажите ООО")
        if not department.strip():
            raise BonusError("Укажите ресторан или офис")
        if not job_title.strip():
            raise BonusError("Укажите должность")
        discount = _discount(discount_percent)
        if discount == 0 and segment.discount_percent:
            discount = segment.discount_percent
        if discount == 0 and template is not None and template.discount_percent:
            discount = template.discount_percent
        one_shot = segment.single_use or ledger_is_single_use(sheet)
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
                discount_percent=discount,
                single_use=one_shot,
                segment_name=segment.name,
                ledger_sheet=sheet.value,
                company=company,
                birth_date=born,
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
        media_on_s3=s3_enabled(),
    )


@router.post("/hr/design/segments")
def segment_create(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    name: str = Form(""),
    accrual_period: str = Form("none"),
    requires_expiry: str = Form("0"),
    single_use: str = Form("0"),
    discount_percent: str = Form(""),
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
            single_use=single_use == "1",
            discount_percent=_discount(discount_percent),
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
    single_use: str = Form("0"),
    discount_percent: str = Form(""),
    csrf_token: str = Form(""),
):
    """Редактирует название, периодичность, срок и процент сегмента."""
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
            single_use=single_use == "1",
            discount_percent=_discount(discount_percent),
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
    discount_percent: str = Form(""),
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
            discount_percent=_discount(discount_percent),
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
    discount_percent: str = Form(""),
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
            discount_percent=_discount(discount_percent),
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
def import_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require(Role.hr))):
    """Загрузка таблицы из старого Statix Bonus."""
    templates = list_templates(db)
    return render(
        request,
        "hr_import.html",
        section="import",
        results=None,
        templates=templates,
        selected_template_id=templates[0].id if templates else 0,
    )


@router.post("/hr/import")
def import_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    table: UploadFile = File(...),
    template_id: int = Form(0),
    csrf_token: str = Form(""),
):
    """Переносит строки таблицы в сертификаты с одним общим дизайном."""
    try:
        guard_csrf(request, csrf_token)
        template = resolve_template(db, template_id)
        payload = table.file.read()
        parsed = parse_table(table.filename or "table.csv", payload)
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse("/hr/import", status_code=303)
    shared_template_id = None if template is None else template.id
    results = [
        _import_row(db, user, row, public_base(request), shared_template_id) for row in parsed
    ]
    return render(
        request,
        "hr_import.html",
        section="import",
        results=results,
        templates=list_templates(db),
        selected_template_id=shared_template_id or 0,
    )


@router.get("/hr/import/sample.xlsx")
def sample_xlsx(user: User = Depends(require(Role.hr))) -> Response:
    """Пример таблицы Excel с актуальными колонками и шириной."""
    return Response(
        content=build_sample_workbook(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=sertifikaty.xlsx"},
    )


@router.get("/hr/import/sample.csv")
def sample_csv(user: User = Depends(require(Role.hr))) -> Response:
    """Старый адрес примера — отдаём актуальный Excel."""
    return sample_xlsx(user)


@router.get("/hr/staff")
def staff_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    q: str = "",
    edit: int = 0,
):
    """Управляющие и сотрудники HR."""
    return render(
        request,
        "hr_staff.html",
        section="staff",
        staff=list_staff(db, q),
        query=q,
        edit_id=edit,
    )


@router.post("/hr/staff")
def staff_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.hr)),
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(""),
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
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            password=password,
            role=Role.hr if role == "hr" else Role.manager,
            restaurant=restaurant,
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse("/hr/staff", status_code=303)
    flash(request, f"Доступ для {created.full_name} создан · логин {created.login}")
    flash(request, plain, "secret")
    return RedirectResponse("/hr/staff", status_code=303)


@router.post("/hr/staff/{user_id}")
def staff_update(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require(Role.hr)),
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(""),
    password: str = Form(""),
    role: str = Form("manager"),
    restaurant: str = Form(""),
    login: str = Form(""),
    q: str = Form(""),
    csrf_token: str = Form(""),
):
    """Сохраняет изменения доступа."""
    person = db.get(User, user_id)
    try:
        guard_csrf(request, csrf_token)
        if person is None or person.role == Role.employee:
            raise BonusError("Пользователь не найден")
        update_staff(
            db,
            person,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            role=Role.hr if role == "hr" else Role.manager,
            restaurant=restaurant,
            login=login,
            password=password,
        )
    except BonusError as exc:
        flash(request, exc.message, "bad")
        return RedirectResponse(_staff_redirect(q, user_id), status_code=303)
    flash(request, "Данные доступа сохранены")
    if password.strip():
        flash(request, password.strip(), "secret")
    return RedirectResponse(_staff_redirect(q), status_code=303)


@router.post("/hr/staff/{user_id}/active")
def staff_active(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require(Role.hr)),
    active: str = Form("0"),
    q: str = Form(""),
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
    return RedirectResponse(_staff_redirect(q), status_code=303)


def _staff_redirect(query: str = "", edit_id: int = 0) -> str:
    """Возвращает на список доступа с поиском и открытой карточкой правки."""
    from urllib.parse import urlencode

    params: dict[str, str] = {}
    cleaned = query.strip()
    if cleaned:
        params["q"] = cleaned
    if edit_id > 0:
        params["edit"] = str(edit_id)
    if not params:
        return "/hr/staff"
    return f"/hr/staff?{urlencode(params)}"


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


def _discount(raw: str) -> int:
    cleaned = raw.strip().replace("%", "")
    if cleaned == "":
        return 0
    try:
        return normalize_discount(int(cleaned))
    except ValueError as exc:
        raise BonusError("Процент — целое число от 0 до 100") from exc


def _import_row(
    db: Session,
    actor: User,
    row,
    base_url: str,
    template_id: int | None,
) -> ImportRowResult:
    """Выпускает одну строку импорта с общим шаблоном оформления."""
    if row.draft is None:
        return ImportRowResult(row_number=row.row_number, ok=False, message=row.error)
    try:
        segment_id = _segment_id_by_name(db, row.draft.segment_name)
        discount = row.draft.discount_percent
        one_shot = row.draft.single_use
        if segment_id is not None:
            segment = db.get(Segment, segment_id)
            if segment is not None:
                if discount == 0 and segment.discount_percent:
                    discount = segment.discount_percent
                one_shot = segment.single_use
        draft = replace(
            row.draft,
            segment_id=segment_id,
            template_id=template_id,
            discount_percent=discount,
            single_use=one_shot,
        )
        issued = issue_certificate(db, draft, actor)
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


def _segment_id_by_name(db: Session, name: str) -> int | None:
    cleaned = " ".join(name.split())
    if cleaned == "":
        return None
    found = db.scalar(select(Segment).where(Segment.name == cleaned))
    if found is None:
        found = db.scalar(select(Segment).where(Segment.name.ilike(cleaned)))
    if found is None:
        raise BonusError(f"Сегмент «{cleaned}» не найден")
    if not found.is_active:
        raise BonusError(f"Сегмент «{cleaned}» скрыт")
    return found.id

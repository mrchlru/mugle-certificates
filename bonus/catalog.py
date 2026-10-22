"""Сегменты и шаблоны оформления карточек."""

import re
import secrets
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import AccrualPeriod, CardTemplate, Certificate, Role, Segment, User
from bonus.services import BonusError, normalize_discount
from bonus.storage import (
    delete_template_object,
    public_media_url,
    read_template_bytes,
    s3_enabled,
    store_template_bytes,
)
from bonus.weekdays import ALL_DAYS, WEEKDAYS, normalize_days

# Натуральный макет баннера 1672×763 — храним половину, та же пропорция.
_BANNER_SIZE = (836, 382)
# На карточке кастомный логотип до 140×44 — храним 2× для Retina.
_LOGO_SIZE = (280, 88)
_DEFAULT_LOGO_URL = "/static/logo.svg"
_WEBP_QUALITY = 78
_LOGO_WEBP_QUALITY = 90
_MAX_UPLOAD_BYTES = 8_000_000

# name, начисление, нужен ли срок, одноразовый
DEFAULT_SEGMENTS: tuple[tuple[str, AccrualPeriod, bool, bool], ...] = (
    ("Ланч", AccrualPeriod.daily, False, False),
    ("Ежемесячные бонусы", AccrualPeriod.monthly, False, False),
    ("Бартер на все рестораны", AccrualPeriod.none, True, False),
    ("Бартер Москва-Стамбул", AccrualPeriod.none, True, False),
    ("Бартер Сибирские Огни", AccrualPeriod.none, True, False),
    ("Бартер Пинчи", AccrualPeriod.none, True, False),
    ("Бартер Си мама", AccrualPeriod.none, True, False),
    ("День рождения", AccrualPeriod.yearly, True, True),
    ("Спасибо", AccrualPeriod.none, True, False),
    ("Подарок на день рождения", AccrualPeriod.none, True, True),
    ("Карта сотрудника", AccrualPeriod.monthly, False, False),
    ("Разовый", AccrualPeriod.none, False, True),
)

# Старые названия из первого сида — подтягиваем периодичность и, где нужно, имя.
_LEGACY_SEGMENTS: dict[str, tuple[str, AccrualPeriod, bool, bool]] = {
    "ланч (ежедневное начисление)": ("Ланч", AccrualPeriod.daily, False, False),
    "ланч": ("Ланч", AccrualPeriod.daily, False, False),
    "ежемесячные бонусы": ("Ежемесячные бонусы", AccrualPeriod.monthly, False, False),
    "бартер на все рестораны": ("Бартер на все рестораны", AccrualPeriod.none, True, False),
    "бартер москва-стамбул": ("Бартер Москва-Стамбул", AccrualPeriod.none, True, False),
    "бартер сибирские огни": ("Бартер Сибирские Огни", AccrualPeriod.none, True, False),
    "бартер пинчи": ("Бартер Пинчи", AccrualPeriod.none, True, False),
    "бартер си мама": ("Бартер Си мама", AccrualPeriod.none, True, False),
    "день рождения": ("День рождения", AccrualPeriod.yearly, True, True),
    "спасибо": ("Спасибо", AccrualPeriod.none, True, False),
    "подарок на день рождения": ("Подарок на день рождения", AccrualPeriod.none, True, True),
    "карта сотрудника": ("Карта сотрудника", AccrualPeriod.monthly, False, False),
    "разовый": ("Разовый", AccrualPeriod.none, False, True),
}

CARD_COLORS: tuple[tuple[str, str], ...] = (
    ("#00A8A8", "Бирюзовый"),
    ("#FF5A00", "Оранжевый"),
    ("#B91C1C", "Красный"),
    ("#1D4ED8", "Синий"),
    ("#111111", "Чёрный"),
    ("#7C3AED", "Фиолетовый"),
)

ACCRUAL_CHOICES: tuple[tuple[str, str], ...] = (
    (AccrualPeriod.none.value, "Без начисления"),
    (AccrualPeriod.daily.value, "Ежедневно: начисление 04:00, сгорание 23:00 МСК"),
    (AccrualPeriod.monthly.value, "Ежемесячно: сброс к номиналу 1-го в 04:00 МСК"),
    (AccrualPeriod.yearly.value, "Раз в год"),
)

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def ensure_default_segments(db: Session) -> None:
    """Кладёт базовые сегменты и подтягивает периодичность у уже созданных."""
    segments = list(db.scalars(select(Segment)).all())
    by_name = {segment.name.casefold(): segment for segment in segments}
    changed = False
    for segment in segments:
        legacy = _LEGACY_SEGMENTS.get(segment.name.casefold())
        if legacy is None:
            continue
        new_name, period, needs_expiry, one_shot = legacy
        target_key = new_name.casefold()
        if segment.name == new_name:
            if _sync_segment_defaults(segment, period, needs_expiry, one_shot):
                changed = True
            continue
        existing_target = by_name.get(target_key)
        if existing_target is None:
            by_name.pop(segment.name.casefold(), None)
            segment.name = new_name
            by_name[target_key] = segment
            changed = True
            _sync_segment_defaults(segment, period, needs_expiry, one_shot)
            continue
        if _sync_segment_defaults(existing_target, period, needs_expiry, one_shot):
            changed = True
        if segment.is_active:
            segment.is_active = False
            changed = True
    for index, (name, period, needs_expiry, one_shot) in enumerate(DEFAULT_SEGMENTS):
        found = by_name.get(name.casefold())
        if found is None:
            created = Segment(
                name=name,
                slug=_unique_slug(db, _slug(name)),
                accrual_period=period,
                requires_expiry=needs_expiry,
                single_use=one_shot,
                use_days=ALL_DAYS,
                accrual_days=WEEKDAYS if period == AccrualPeriod.daily else WEEKDAYS,
                sort_order=index,
                is_active=True,
            )
            db.add(created)
            by_name[name.casefold()] = created
            changed = True
            continue
        touched = _sync_segment_defaults(found, period, needs_expiry, one_shot)
        if found.sort_order != index:
            found.sort_order = index
            touched = True
        if touched:
            changed = True
    if changed:
        db.commit()


def list_segments(db: Session, *, active_only: bool = True) -> list[Segment]:
    """Сегменты для фильтра и формы выпуска."""
    statement = select(Segment).order_by(Segment.sort_order, Segment.id)
    if active_only:
        statement = statement.where(Segment.is_active.is_(True))
    return list(db.scalars(statement).all())


def list_templates(db: Session, *, active_only: bool = True) -> list[CardTemplate]:
    """Шаблоны-картинки для баннера карточки."""
    statement = select(CardTemplate).order_by(CardTemplate.id.desc())
    if active_only:
        statement = statement.where(CardTemplate.is_active.is_(True))
    return list(db.scalars(statement).all())


def parse_accrual_period(raw: str) -> AccrualPeriod:
    """Читает периодичность из формы."""
    try:
        return AccrualPeriod(raw.strip() or AccrualPeriod.none.value)
    except ValueError as exc:
        raise BonusError("Неизвестная периодичность начисления") from exc


def create_segment(
    db: Session,
    name: str,
    accrual_period: AccrualPeriod = AccrualPeriod.none,
    *,
    requires_expiry: bool = False,
    single_use: bool = False,
    birthday_unwrap: bool = False,
    discount_percent: int = 0,
    use_days: object = ALL_DAYS,
    accrual_days: object = WEEKDAYS,
) -> Segment:
    """Добавляет новый сегмент."""
    cleaned = " ".join(name.split())
    if len(cleaned) < 2:
        raise BonusError("Укажите название сегмента")
    if db.scalar(select(Segment.id).where(Segment.name == cleaned)) is not None:
        raise BonusError("Такой сегмент уже есть")
    segment = Segment(
        name=cleaned[:120],
        slug=_unique_slug(db, _slug(cleaned)),
        accrual_period=accrual_period,
        requires_expiry=requires_expiry,
        single_use=single_use,
        birthday_unwrap=birthday_unwrap,
        discount_percent=normalize_discount(discount_percent),
        use_days=normalize_days(use_days, default=ALL_DAYS),
        accrual_days=normalize_days(accrual_days, default=WEEKDAYS),
        sort_order=1000 + int(db.scalar(select(Segment.id).order_by(Segment.id.desc()).limit(1)) or 0),
        is_active=True,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def update_segment(
    db: Session,
    segment: Segment,
    *,
    name: str,
    accrual_period: AccrualPeriod,
    requires_expiry: bool,
    single_use: bool = False,
    birthday_unwrap: bool = False,
    discount_percent: int = 0,
    use_days: object = ALL_DAYS,
    accrual_days: object = WEEKDAYS,
) -> Segment:
    """Меняет название, периодичность, срок, дни и процент."""
    cleaned = " ".join(name.split())
    if len(cleaned) < 2:
        raise BonusError("Укажите название сегмента")
    twin = db.scalar(select(Segment.id).where(Segment.name == cleaned, Segment.id != segment.id))
    if twin is not None:
        raise BonusError("Сегмент с таким названием уже есть")
    segment.name = cleaned[:120]
    segment.accrual_period = accrual_period
    segment.requires_expiry = requires_expiry
    segment.single_use = single_use
    segment.birthday_unwrap = birthday_unwrap
    segment.discount_percent = normalize_discount(discount_percent)
    segment.use_days = normalize_days(use_days, default=ALL_DAYS)
    segment.accrual_days = normalize_days(accrual_days, default=WEEKDAYS)
    db.commit()
    db.refresh(segment)
    return segment


def set_segment_active(db: Session, segment: Segment, active: bool) -> None:
    """Прячет или снова показывает сегмент в формах."""
    segment.is_active = active
    db.commit()


def save_template(
    db: Session,
    *,
    name: str,
    filename: str,
    payload: bytes,
    content_type: str,
    logo_filename: str = "",
    logo_payload: bytes | None = None,
    logo_content_type: str = "",
    discount_percent: int = 0,
    color: str = "#00A8A8",
    segment_id: int | None = None,
) -> CardTemplate:
    """Сохраняет баннер; без логотипа на карточке будет дефолтный."""
    cleaned = " ".join(name.split()) or Path(filename).stem or "Шаблон"
    banner_path = _store_banner_upload(filename, payload, content_type)
    logo_path = ""
    if logo_payload:
        logo_path = _store_logo_upload(logo_filename, logo_payload, logo_content_type)
    template = CardTemplate(
        name=cleaned[:120],
        image_path=banner_path,
        logo_path=logo_path,
        color=normalize_color(color or "#00A8A8"),
        segment_id=_optional_segment_id(db, segment_id),
        discount_percent=normalize_discount(discount_percent),
        is_active=True,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def set_template_active(db: Session, template: CardTemplate, active: bool) -> None:
    """Отключает шаблон без удаления файла."""
    template.is_active = active
    db.commit()


def delete_template(db: Session, template: CardTemplate, actor: User) -> str:
    """Полностью удаляет шаблон и файлы. Только admin."""
    if actor.role != Role.admin:
        raise BonusError("Удалять шаблоны может только главный администратор")
    name = template.name
    banner_path = template.image_path or ""
    logo_path = (template.logo_path or "").strip()
    template_id = template.id
    db.execute(
        update(Certificate)
        .where(Certificate.template_id == template_id)
        .values(template_id=None)
    )
    db.delete(template)
    db.commit()
    if banner_path:
        delete_template_object(banner_path)
    if logo_path:
        delete_template_object(logo_path)
    return name


def update_template(
    db: Session,
    template: CardTemplate,
    *,
    name: str,
    discount_percent: int = 0,
    color: str = "#00A8A8",
    segment_id: int | None = None,
    filename: str = "",
    payload: bytes | None = None,
    content_type: str = "",
    logo_filename: str = "",
    logo_payload: bytes | None = None,
    logo_content_type: str = "",
) -> CardTemplate:
    """Меняет название, цвет, сегмент и при необходимости баннер/логотип."""
    cleaned = " ".join(name.split()) or template.name
    if len(cleaned) < 1:
        raise BonusError("Укажите название шаблона")
    template.name = cleaned[:120]
    template.discount_percent = normalize_discount(discount_percent)
    template.color = normalize_color(color or "#00A8A8")
    template.segment_id = _optional_segment_id(db, segment_id)
    old_banner = ""
    old_logo = ""
    if payload is not None and len(payload) > 0:
        stored = _store_banner_upload(filename, payload, content_type)
        old_banner = template.image_path
        template.image_path = stored
    if logo_payload is not None and len(logo_payload) > 0:
        stored_logo = _store_logo_upload(logo_filename, logo_payload, logo_content_type)
        old_logo = template.logo_path or ""
        template.logo_path = stored_logo
    db.commit()
    db.refresh(template)
    if old_banner and old_banner != template.image_path:
        delete_template_object(old_banner)
    if old_logo and old_logo != template.logo_path:
        delete_template_object(old_logo)
    return template


def _optional_segment_id(db: Session, segment_id: int | None) -> int | None:
    """Пустая привязка или id активного сегмента."""
    if segment_id is None or segment_id <= 0:
        return None
    segment = db.get(Segment, segment_id)
    if segment is None or not segment.is_active:
        raise BonusError("Выберите сегмент для шаблона или оставьте без привязки")
    return segment.id


def template_logo_url(template: CardTemplate | None) -> str:
    """Публичный URL логотипа шаблона или дефолтный Mugle."""
    if template is None:
        return _DEFAULT_LOGO_URL
    path = (template.logo_path or "").strip()
    if not path:
        return _DEFAULT_LOGO_URL
    return media_url(path)


def template_logo_is_custom(template: CardTemplate | None) -> bool:
    """Есть ли у шаблона свой загруженный логотип."""
    return bool(template is not None and (template.logo_path or "").strip())


def optimize_existing_templates(db: Session) -> None:
    """Пережимает локальные баннеры и при необходимости выгружает в S3."""
    changed = False
    use_s3 = s3_enabled()
    for template in db.scalars(select(CardTemplate)).all():
        if template.image_path.startswith(("http://", "https://")):
            continue
        raw = read_template_bytes(template.image_path)
        if raw is None:
            continue
        needs_recompress = not (template.image_path.endswith(".webp") and len(raw) <= 120_000)
        needs_s3_upload = use_s3
        if not needs_recompress and not needs_s3_upload:
            continue
        try:
            optimized = _optimize_template_image(raw) if needs_recompress else raw
        except BonusError:
            continue
        old_name = template.image_path
        stored = store_template_bytes(optimized, f"{secrets.token_hex(12)}.webp")
        template.image_path = stored
        changed = True
        if old_name != stored:
            delete_template_object(old_name)
    if changed:
        db.commit()


def normalize_color(value: str) -> str:
    """Проверяет цвет карточки."""
    color = value.strip()
    if not _HEX.fullmatch(color):
        raise BonusError("Цвет карточки — в формате #RRGGBB")
    return color.upper()


def resolve_segment(db: Session, segment_id: int) -> Segment:
    """Достаёт активный сегмент или объясняет, что его нет."""
    segment = db.get(Segment, segment_id)
    if segment is None or not segment.is_active:
        raise BonusError("Выберите сегмент")
    return segment


def resolve_template(db: Session, template_id: int | None) -> CardTemplate | None:
    """Шаблон можно не выбирать — тогда баннер будет пустым."""
    if template_id is None or template_id <= 0:
        return None
    template = db.get(CardTemplate, template_id)
    if template is None or not template.is_active:
        raise BonusError("Выберите шаблон или оставьте без картинки")
    return template


def media_url(image_path: str) -> str:
    """Адрес загруженной картинки шаблона (локально или CDN/S3)."""
    return public_media_url(image_path)


def accrual_label(period: AccrualPeriod | str) -> str:
    """Подпись периодичности для интерфейса."""
    key = period.value if isinstance(period, AccrualPeriod) else period
    for value, title in ACCRUAL_CHOICES:
        if value == key:
            return title
    return key


def _sync_segment_defaults(
    segment: Segment,
    period: AccrualPeriod,
    requires_expiry: bool,
    single_use: bool = False,
) -> bool:
    """Обновляет период, срок и одноразовость у сегмента из каталога по умолчанию."""
    changed = False
    if segment.accrual_period != period:
        segment.accrual_period = period
        changed = True
    if segment.requires_expiry != requires_expiry:
        segment.requires_expiry = requires_expiry
        changed = True
    if segment.single_use != single_use:
        segment.single_use = single_use
        changed = True
    return changed


def _store_banner_upload(filename: str, payload: bytes, content_type: str) -> str:
    """Проверяет баннер и сохраняет оптимизированный WebP."""
    _validate_image_upload(payload, content_type, label="Баннер")
    optimized = _optimize_template_image(payload)
    return store_template_bytes(optimized, f"{secrets.token_hex(12)}.webp")


def _store_logo_upload(filename: str, payload: bytes, content_type: str) -> str:
    """Проверяет логотип и сохраняет WebP 280×88 (на карточке до 140×44)."""
    _validate_image_upload(payload, content_type, label="Логотип")
    optimized = _optimize_logo_image(payload)
    return store_template_bytes(optimized, f"logo-{secrets.token_hex(12)}.webp")


def _validate_image_upload(payload: bytes, content_type: str, *, label: str) -> None:
    """Общие проверки JPEG/PNG/WebP до обработки."""
    if content_type not in _IMAGE_TYPES:
        raise BonusError(f"{label}: нужен JPEG, PNG или WebP")
    if len(payload) > _MAX_UPLOAD_BYTES:
        raise BonusError(f"{label}: файл больше 8 МБ")
    if not payload:
        raise BonusError(f"{label}: файл пустой")


def _optimize_template_image(payload: bytes) -> bytes:
    """Режет баннер под карточку и сжимает в WebP."""
    try:
        with Image.open(BytesIO(payload)) as image:
            framed = ImageOps.exif_transpose(image)
            rgb = framed.convert("RGB")
            fitted = ImageOps.fit(
                rgb,
                _BANNER_SIZE,
                method=Image.Resampling.LANCZOS,
            )
            buffer = BytesIO()
            fitted.save(
                buffer,
                format="WEBP",
                quality=_WEBP_QUALITY,
                method=6,
            )
    except UnidentifiedImageError as exc:
        raise BonusError("Файл не похож на картинку") from exc
    except OSError as exc:
        raise BonusError("Не удалось обработать картинку") from exc
    data = buffer.getvalue()
    if not data:
        raise BonusError("Не удалось обработать картинку")
    return data


def _optimize_logo_image(payload: bytes) -> bytes:
    """Обрезает пустые поля и вписывает логотип в 280×88 с прозрачностью."""
    try:
        with Image.open(BytesIO(payload)) as image:
            framed = ImageOps.exif_transpose(image)
            rgba = _trim_logo_margins(framed.convert("RGBA"))
            fitted = ImageOps.contain(
                rgba,
                _LOGO_SIZE,
                method=Image.Resampling.LANCZOS,
            )
            canvas = Image.new("RGBA", _LOGO_SIZE, (0, 0, 0, 0))
            offset = (0, (_LOGO_SIZE[1] - fitted.height) // 2)
            canvas.paste(fitted, offset, fitted)
            buffer = BytesIO()
            canvas.save(
                buffer,
                format="WEBP",
                quality=_LOGO_WEBP_QUALITY,
                method=6,
            )
    except UnidentifiedImageError as exc:
        raise BonusError("Логотип: файл не похож на картинку") from exc
    except OSError as exc:
        raise BonusError("Логотип: не удалось обработать картинку") from exc
    data = buffer.getvalue()
    if not data:
        raise BonusError("Логотип: не удалось обработать картинку")
    return data


def _trim_logo_margins(image: Image.Image) -> Image.Image:
    """Срезает прозрачные и почти чёрные поля, из‑за которых логотип сжимается."""
    alpha = image.split()[-1]
    bbox = alpha.getbbox()
    if bbox is None:
        return image
    cropped = image.crop(bbox)
    pixels = cropped.load()
    width, height = cropped.size
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha_value = pixels[x, y]
            if alpha_value > 12 and (red > 28 or green > 28 or blue > 28):
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y
    if max_x < 0:
        return cropped
    return cropped.crop((min_x, min_y, max_x + 1, max_y + 1))


def _slug(name: str) -> str:
    mapped = "".join(_TRANSLIT.get(char, char) for char in name.casefold())
    cleaned = re.sub(r"[^a-z0-9]+", "-", mapped).strip("-")
    return (cleaned or "segment")[:80]


def _unique_slug(db: Session, base: str) -> str:
    candidate = base
    number = 2
    while db.scalar(select(Segment.id).where(Segment.slug == candidate)) is not None:
        candidate = f"{base}-{number}"
        number += 1
    return candidate


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

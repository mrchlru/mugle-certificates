"""Сегменты и шаблоны оформления карточек."""

import re
import secrets
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import AccrualPeriod, CardTemplate, Segment
from bonus.services import BonusError
from bonus.storage import (
    delete_template_object,
    public_media_url,
    read_template_bytes,
    s3_enabled,
    store_template_bytes,
)

_BANNER_SIZE = (720, 264)
_WEBP_QUALITY = 78
_MAX_UPLOAD_BYTES = 8_000_000

# name, начисление, нужен ли срок действия на карточке
DEFAULT_SEGMENTS: tuple[tuple[str, AccrualPeriod, bool], ...] = (
    ("Ланч", AccrualPeriod.daily, False),
    ("Ежемесячные бонусы", AccrualPeriod.monthly, False),
    ("Бартер на все рестораны", AccrualPeriod.none, True),
    ("Бартер Москва-Стамбул", AccrualPeriod.none, True),
    ("Бартер Сибирские Огни", AccrualPeriod.none, True),
    ("Бартер Пинчи", AccrualPeriod.none, True),
    ("Бартер Си мама", AccrualPeriod.none, True),
    ("День рождения", AccrualPeriod.yearly, True),
)

# Старые названия из первого сида — подтягиваем периодичность и, где нужно, имя.
_LEGACY_SEGMENTS: dict[str, tuple[str, AccrualPeriod, bool]] = {
    "ланч (ежедневное начисление)": ("Ланч", AccrualPeriod.daily, False),
    "ланч": ("Ланч", AccrualPeriod.daily, False),
    "ежемесячные бонусы": ("Ежемесячные бонусы", AccrualPeriod.monthly, False),
    "бартер на все рестораны": ("Бартер на все рестораны", AccrualPeriod.none, True),
    "бартер москва-стамбул": ("Бартер Москва-Стамбул", AccrualPeriod.none, True),
    "бартер сибирские огни": ("Бартер Сибирские Огни", AccrualPeriod.none, True),
    "бартер пинчи": ("Бартер Пинчи", AccrualPeriod.none, True),
    "бартер си мама": ("Бартер Си мама", AccrualPeriod.none, True),
    "день рождения": ("День рождения", AccrualPeriod.yearly, True),
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
    (AccrualPeriod.daily.value, "Ежедневно"),
    (AccrualPeriod.monthly.value, "Ежемесячно"),
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
        new_name, period, needs_expiry = legacy
        target_key = new_name.casefold()
        if segment.name == new_name:
            if _sync_segment_defaults(segment, period, needs_expiry):
                changed = True
            continue
        existing_target = by_name.get(target_key)
        if existing_target is None:
            by_name.pop(segment.name.casefold(), None)
            segment.name = new_name
            by_name[target_key] = segment
            changed = True
            _sync_segment_defaults(segment, period, needs_expiry)
            continue
        if _sync_segment_defaults(existing_target, period, needs_expiry):
            changed = True
        if segment.is_active:
            segment.is_active = False
            changed = True
    for index, (name, period, needs_expiry) in enumerate(DEFAULT_SEGMENTS):
        found = by_name.get(name.casefold())
        if found is None:
            created = Segment(
                name=name,
                slug=_unique_slug(db, _slug(name)),
                accrual_period=period,
                requires_expiry=needs_expiry,
                sort_order=index,
                is_active=True,
            )
            db.add(created)
            by_name[name.casefold()] = created
            changed = True
            continue
        touched = _sync_segment_defaults(found, period, needs_expiry)
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
) -> Segment:
    """Меняет название, периодичность и признак срока действия."""
    cleaned = " ".join(name.split())
    if len(cleaned) < 2:
        raise BonusError("Укажите название сегмента")
    twin = db.scalar(select(Segment.id).where(Segment.name == cleaned, Segment.id != segment.id))
    if twin is not None:
        raise BonusError("Сегмент с таким названием уже есть")
    segment.name = cleaned[:120]
    segment.accrual_period = accrual_period
    segment.requires_expiry = requires_expiry
    db.commit()
    db.refresh(segment)
    return segment


def set_segment_active(db: Session, segment: Segment, active: bool) -> None:
    """Прячет или снова показывает сегмент в формах."""
    segment.is_active = active
    db.commit()


def save_template(db: Session, *, name: str, filename: str, payload: bytes, content_type: str) -> CardTemplate:
    """Сохраняет картинку шаблона на диск или в S3 и пишет путь в базу."""
    cleaned = " ".join(name.split()) or Path(filename).stem or "Шаблон"
    if content_type not in _IMAGE_TYPES:
        raise BonusError("Нужен JPEG, PNG или WebP")
    if len(payload) > _MAX_UPLOAD_BYTES:
        raise BonusError("Картинка больше 8 МБ")
    optimized = _optimize_template_image(payload)
    stored = store_template_bytes(optimized, f"{secrets.token_hex(12)}.webp")
    template = CardTemplate(name=cleaned[:120], image_path=stored, is_active=True)
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def set_template_active(db: Session, template: CardTemplate, active: bool) -> None:
    """Отключает шаблон без удаления файла."""
    template.is_active = active
    db.commit()


def update_template(
    db: Session,
    template: CardTemplate,
    *,
    name: str,
    filename: str = "",
    payload: bytes | None = None,
    content_type: str = "",
) -> CardTemplate:
    """Меняет название и при необходимости заменяет картинку, удаляя старую."""
    cleaned = " ".join(name.split()) or template.name
    if len(cleaned) < 1:
        raise BonusError("Укажите название шаблона")
    template.name = cleaned[:120]
    if payload is not None and len(payload) > 0:
        if content_type not in _IMAGE_TYPES:
            raise BonusError("Нужен JPEG, PNG или WebP")
        if len(payload) > _MAX_UPLOAD_BYTES:
            raise BonusError("Картинка больше 8 МБ")
        optimized = _optimize_template_image(payload)
        stored = store_template_bytes(optimized, f"{secrets.token_hex(12)}.webp")
        old_path = template.image_path
        template.image_path = stored
        db.commit()
        db.refresh(template)
        if old_path and old_path != stored:
            delete_template_object(old_path)
        return template
    db.commit()
    db.refresh(template)
    return template


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


def _sync_segment_defaults(segment: Segment, period: AccrualPeriod, requires_expiry: bool) -> bool:
    """Обновляет период и признак срока у сегмента из каталога по умолчанию."""
    changed = False
    if segment.accrual_period != period:
        segment.accrual_period = period
        changed = True
    if segment.requires_expiry != requires_expiry:
        segment.requires_expiry = requires_expiry
        changed = True
    return changed


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

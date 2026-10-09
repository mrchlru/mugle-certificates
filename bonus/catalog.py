"""Сегменты и шаблоны оформления карточек."""

import re
import secrets
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from bonus.config import get_settings
from bonus.models import CardTemplate, Segment
from bonus.services import BonusError

DEFAULT_SEGMENTS: tuple[str, ...] = (
    "Ланч (ежедневное начисление)",
    "Ежемесячные бонусы",
    "Бартер на все рестораны",
    "Бартер Москва-Стамбул",
    "Бартер Сибирские Огни",
    "Бартер Пинчи",
    "Бартер Си мама",
    "День рождения",
)

CARD_COLORS: tuple[tuple[str, str], ...] = (
    ("#00A8A8", "Бирюзовый"),
    ("#FF5A00", "Оранжевый"),
    ("#B91C1C", "Красный"),
    ("#1D4ED8", "Синий"),
    ("#111111", "Чёрный"),
    ("#7C3AED", "Фиолетовый"),
)

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def ensure_default_segments(db: Session) -> None:
    """Кладёт базовые сегменты, если их ещё нет."""
    existing = {name.casefold() for name in db.scalars(select(Segment.name)).all()}
    for index, name in enumerate(DEFAULT_SEGMENTS):
        if name.casefold() in existing:
            continue
        db.add(Segment(name=name, slug=_slug(name), sort_order=index, is_active=True))
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


def create_segment(db: Session, name: str) -> Segment:
    """Добавляет новый сегмент."""
    cleaned = " ".join(name.split())
    if len(cleaned) < 2:
        raise BonusError("Укажите название сегмента")
    if db.scalar(select(Segment.id).where(Segment.name == cleaned)) is not None:
        raise BonusError("Такой сегмент уже есть")
    segment = Segment(
        name=cleaned[:120],
        slug=_unique_slug(db, _slug(cleaned)),
        sort_order=1000 + int(db.scalar(select(Segment.id).order_by(Segment.id.desc()).limit(1)) or 0),
        is_active=True,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def set_segment_active(db: Session, segment: Segment, active: bool) -> None:
    """Прячет или снова показывает сегмент в формах."""
    segment.is_active = active
    db.commit()


def save_template(db: Session, *, name: str, filename: str, payload: bytes, content_type: str) -> CardTemplate:
    """Сохраняет картинку шаблона на диск и в базу."""
    cleaned = " ".join(name.split()) or Path(filename).stem or "Шаблон"
    extension = _IMAGE_TYPES.get(content_type)
    if extension is None:
        raise BonusError("Нужен JPEG, PNG или WebP")
    if len(payload) > 3_000_000:
        raise BonusError("Картинка больше 3 МБ")
    folder = _uploads_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stored = f"{secrets.token_hex(12)}{extension}"
    (folder / stored).write_bytes(payload)
    template = CardTemplate(name=cleaned[:120], image_path=stored, is_active=True)
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def set_template_active(db: Session, template: CardTemplate, active: bool) -> None:
    """Отключает шаблон без удаления файла."""
    template.is_active = active
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
    """Адрес загруженной картинки шаблона."""
    return f"/media/{image_path}"


def _uploads_dir() -> Path:
    return get_settings().data_path() / "uploads"


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

"""Дни недели для использования карты и ежедневного начисления."""

from __future__ import annotations

WEEKDAY_CHOICES: tuple[tuple[str, str], ...] = (
    ("1", "Пн"),
    ("2", "Вт"),
    ("3", "Ср"),
    ("4", "Чт"),
    ("5", "Пт"),
    ("6", "Сб"),
    ("7", "Вс"),
)

ALL_DAYS = "1234567"
WEEKDAYS = "12345"


def normalize_days(raw: object, *, default: str = ALL_DAYS) -> str:
    """Собирает строку дней 1–7 из формы или сохранённого значения."""
    if raw is None:
        return default
    if isinstance(raw, (list, tuple, set)):
        tokens = [str(item).strip() for item in raw]
    else:
        text = str(raw).strip()
        if not text:
            return default
        tokens = [part.strip() for part in text.replace(",", " ").split()]
        if len(tokens) == 1 and tokens[0].isdigit() and len(tokens[0]) > 1:
            tokens = list(tokens[0])
    chosen = sorted({token for token in tokens if token in {value for value, _ in WEEKDAY_CHOICES}})
    if not chosen:
        return default
    return "".join(chosen)


def day_set(raw: str | None, *, default: str = ALL_DAYS) -> set[str]:
    """Множество выбранных дней."""
    return set(normalize_days(raw, default=default))


def allows_weekday(raw: str | None, iso_weekday: int, *, default: str = ALL_DAYS) -> bool:
    """Проверяет, входит ли ISO-день (1=Пн … 7=Вс) в маску."""
    key = str(iso_weekday)
    return key in day_set(raw, default=default)


def days_label(raw: str | None, *, default: str = ALL_DAYS) -> str:
    """Короткая подпись: «Пн–Пт» или «Пн, Ср, Пт»."""
    normalized = normalize_days(raw, default=default)
    if normalized == ALL_DAYS:
        return "Все дни"
    if normalized == WEEKDAYS:
        return "Пн–Пт"
    titles = dict(WEEKDAY_CHOICES)
    return ", ".join(titles[day] for day in normalized)

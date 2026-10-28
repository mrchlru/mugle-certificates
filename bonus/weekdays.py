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


def normalize_single_weekday(raw: object, *, default: str = "5") -> str:
    """Один ISO-день 1–7 (для дня сгорания еженедельной карты)."""
    chosen = normalize_days(raw, default=default)
    return chosen[-1] if chosen else default


_DAY_NAME_ALIASES: dict[str, str] = {
    "1": "1",
    "пн": "1",
    "понедельник": "1",
    "mon": "1",
    "monday": "1",
    "2": "2",
    "вт": "2",
    "вторник": "2",
    "tue": "2",
    "tuesday": "2",
    "3": "3",
    "ср": "3",
    "среда": "3",
    "wed": "3",
    "wednesday": "3",
    "4": "4",
    "чт": "4",
    "четверг": "4",
    "thu": "4",
    "thursday": "4",
    "5": "5",
    "пт": "5",
    "пятница": "5",
    "fri": "5",
    "friday": "5",
    "6": "6",
    "сб": "6",
    "суббота": "6",
    "sat": "6",
    "saturday": "6",
    "7": "7",
    "вс": "7",
    "воскресенье": "7",
    "sun": "7",
    "sunday": "7",
}


def parse_weekday_labels(raw: object, *, default: str = ALL_DAYS) -> str:
    """Маска дней из подписей «Пн,Вт,Ср» или цифр 1–7. Пусто → default."""
    if raw is None:
        return default
    text = str(raw).strip()
    if text == "":
        return default
    tokens = [part.strip() for part in text.replace(";", ",").replace("/", ",").split(",")]
    flat: list[str] = []
    for token in tokens:
        if not token:
            continue
        pieces = token.split() if " " in token and len(token) > 3 else [token]
        flat.extend(pieces)
    chosen: list[str] = []
    for token in flat:
        key = "".join(ch for ch in token.casefold().replace("ё", "е") if ch.isalnum())
        day = _DAY_NAME_ALIASES.get(key)
        if day is None:
            raise ValueError(token)
        if day not in chosen:
            chosen.append(day)
    if not chosen:
        return default
    return "".join(sorted(chosen, key=int))


def days_label(raw: str | None, *, default: str = ALL_DAYS) -> str:
    """Короткая подпись: «Пн–Пт» или «Пн, Ср, Пт»."""
    normalized = normalize_days(raw, default=default)
    if normalized == ALL_DAYS:
        return "Все дни"
    if normalized == WEEKDAYS:
        return "Пн–Пт"
    titles = dict(WEEKDAY_CHOICES)
    return ", ".join(titles[day] for day in normalized)

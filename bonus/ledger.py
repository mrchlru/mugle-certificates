"""Листы учёта сертификатов в Google Таблице (как в Excel Statix)."""

from enum import Enum


class LedgerSheet(str, Enum):
    """Куда писать карту в Google Sheets."""

    barter = "barter"
    thanks = "thanks"
    gift_birthday = "gift_birthday"
    birthday = "birthday"
    employee = "employee"


# title в таблице, заголовки строки 1, требует ли дату рождения, разовый ли
LEDGER_SPECS: dict[LedgerSheet, tuple[str, tuple[str, ...], bool, bool]] = {
    LedgerSheet.barter: (
        "бартер",
        (
            "Номер карты",
            "Дата выпуска",
            "ФИО Сотрудника",
            "ООО",
            "Ресторан/офис",
            "Должность",
            "повод/бартер",
            "срок действия до",
            "Сумма сертификата",
            "Дата Использования",
            "Сумма",
            "Ресторан",
            "Комментарии",
        ),
        False,
        False,
    ),
    LedgerSheet.thanks: (
        "сертификаты для Спасибо",
        (
            "Номер карты",
            "Дата выпуска",
            "ФИО Сотрудника",
            "ООО",
            "Ресторан/офис",
            "Должность",
            "повод",
            "срок действия до",
            "Сумма сертификата",
            "Дата Использования",
            "Сумма",
            "Ресторан",
            "Комментарии",
        ),
        False,
        False,
    ),
    LedgerSheet.gift_birthday: (
        "подарочные сертификаты на др",
        (
            "Номер",
            "ФИО",
            "ООО",
            "Ресторан/офис",
            "Должность",
            "Дата рождения",
            "Сумма сертификата",
            "Дата Использования",
            "Сумма",
            "Ресторан",
            "Комментарии",
        ),
        True,
        True,
    ),
    LedgerSheet.birthday: (
        "дни рождения",
        (
            "номер",
            "ФИО Сотрудника",
            "ООО",
            "Ресторан/офис",
            "Должность",
            "Дата рождения",
            "Сумма сертификата",
            "Дата Использования",
            "Сумма",
            "Ресторан",
            "Комментарии",
        ),
        True,
        True,
    ),
    LedgerSheet.employee: (
        "карты сотрудников",
        (
            "Номер карты",
            "Дата выпуска",
            "ФИО Сотрудника",
            "ООО",
            "Ресторан/офис",
            "Должность",
            "сегмент",
            "срок действия до",
            "Сумма сертификата",
            "Дата Использования",
            "Сумма",
            "Ресторан",
            "Комментарии",
        ),
        False,
        False,
    ),
}

LEDGER_CHOICES: tuple[tuple[str, str], ...] = (
    (LedgerSheet.barter.value, "Бартер"),
    (LedgerSheet.thanks.value, "Сертификаты для «Спасибо»"),
    (LedgerSheet.gift_birthday.value, "Подарочные на день рождения"),
    (LedgerSheet.birthday.value, "Дни рождения"),
    (LedgerSheet.employee.value, "Карты сотрудников"),
)


def parse_ledger_sheet(value: str) -> LedgerSheet:
    """Разбирает значение переключателя из формы."""
    cleaned = (value or "").strip()
    try:
        return LedgerSheet(cleaned)
    except ValueError as exc:
        raise ValueError("Выберите лист учёта") from exc


def ledger_title(kind: LedgerSheet) -> str:
    """Имя вкладки Google Sheets."""
    return LEDGER_SPECS[kind][0]


def ledger_headers(kind: LedgerSheet) -> tuple[str, ...]:
    """Заголовки колонок листа."""
    return LEDGER_SPECS[kind][1]


def ledger_needs_birth_date(kind: LedgerSheet) -> bool:
    """Нужна ли дата рождения при выпуске."""
    return LEDGER_SPECS[kind][2]


def ledger_is_single_use(kind: LedgerSheet) -> bool:
    """Карты этого листа одноразовые: после первого списания остаток сгорает."""
    return LEDGER_SPECS[kind][3]

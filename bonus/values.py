"""Деньги, даты и подписи, которые видит человек."""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bonus.models import CertificateStatus, TxType

MSK = ZoneInfo("Europe/Moscow")
MAX_KOPECKS = 1_000_000_000


class MoneyError(Exception):
    """Сумму или дату не удалось разобрать."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def parse_money(value: object, *, blank_as_none: bool = False) -> int | None:
    """Переводит сумму в копейки.

    Принимает число или строку с пробелами, запятой и знаком рубля.
    Три знака после точки считаются разделителем тысяч: 10.000 → 10000 ₽.
    """
    if value is None or str(value).strip() == "":
        if blank_as_none:
            return None
        raise MoneyError("Не указана сумма")
    if isinstance(value, bool):
        raise MoneyError("Не понял сумму")
    if isinstance(value, int):
        return _guard_kopecks(value * 100)
    if isinstance(value, float):
        return _guard_kopecks(int((Decimal(str(value)) * 100).quantize(Decimal("1"))))
    text = _normalize_money_text(str(value))
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyError("Не понял сумму. Пример: 1500 или 1500,50") from exc
    return _guard_kopecks(int((amount * 100).quantize(Decimal("1"))))


def format_money(kopecks: int) -> str:
    """Форматирует копейки как рубли: 12 500 ₽ или 12 500,50 ₽."""
    sign = "−" if kopecks < 0 else ""
    absolute = abs(int(kopecks))
    whole = absolute // 100
    frac = absolute % 100
    grouped = f"{whole:,}".replace(",", " ")
    if frac == 0:
        return f"{sign}{grouped} ₽"
    return f"{sign}{grouped},{frac:02d} ₽"


def parse_date(value: object) -> date | None:
    """Читает дату ДД.ММ.ГГГГ, ГГГГ-ММ-ДД или объект даты."""
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise MoneyError("Не понял дату. Нужен формат ДД.ММ.ГГГГ")


def today_msk() -> date:
    """Сегодня по Москве. По этому дню решается, истёк ли сертификат."""
    return datetime.now(MSK).date()


def as_utc(value: datetime) -> datetime:
    """Делает метку времени осведомлённой о часовом поясе UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_dt(value: datetime) -> str:
    """Показывает время по Москве."""
    local = as_utc(value).astimezone(MSK)
    return local.strftime("%d.%m.%Y %H:%M")


def format_date(value: date | None) -> str:
    """Показывает календарную дату для карточки и админки."""
    if value is None:
        return ""
    return value.strftime("%d.%m.%Y")


def format_short_code(code: str) -> str:
    """Показывает короткий код группами: 1234-5678 или старый буквенный."""
    raw = code.replace("-", "").replace(" ", "")
    if raw.isdigit() and len(raw) == 8:
        return f"{raw[:4]}-{raw[4:]}"
    upper = raw.upper()
    if len(upper) == 6:
        return f"{upper[:3]}-{upper[3:]}"
    return upper


def status_label(
    status: CertificateStatus | str,
    balance: int,
    expires_on: date | None,
    single_use: bool = False,
) -> str:
    """Короткая подпись состояния сертификата."""
    if status == CertificateStatus.blocked or status == CertificateStatus.blocked.value:
        return "Заблокирован"
    if expires_on is not None and expires_on < today_msk():
        return "Срок истёк"
    if balance <= 0:
        return "Использован" if single_use else "Израсходован"
    if single_use:
        return "Активен · разовое использование"
    return "Активен"


def tx_label(tx_type: TxType | str, reversal_of_id: int | None) -> str:
    """Название операции в журнале."""
    if reversal_of_id is not None:
        return "Отмена списания"
    labels = {
        TxType.issue.value: "Выпуск",
        TxType.debit.value: "Списание",
        TxType.credit.value: "Начисление",
        TxType.block.value: "Блокировка",
        TxType.unblock.value: "Разблокировка",
        TxType.migrate.value: "Перенос остатка",
        TxType.expire.value: "Списание по сроку",
        TxType.burn.value: "Сгорание остатка",
    }
    key = tx_type.value if isinstance(tx_type, TxType) else tx_type
    return labels.get(key, key)


def _normalize_money_text(value: str) -> str:
    text = value.strip().replace(" ", "").replace("\u00a0", "").replace("₽", "")
    text = text.replace(",", ".")
    if text.count(".") > 1:
        text = text.replace(".", "")
        return text
    if text.count(".") == 1:
        left, right = text.split(".")
        if len(right) == 3 and left.isdigit() and right.isdigit():
            return left + right
    return text


def _guard_kopecks(kopecks: int) -> int:
    if kopecks < 0:
        raise MoneyError("Сумма не может быть отрицательной")
    if kopecks > MAX_KOPECKS:
        raise MoneyError("Слишком большая сумма")
    return kopecks

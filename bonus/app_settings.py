"""Глобальные настройки приложения из таблицы app_settings."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from bonus.models import AppSettings, Certificate


def get_app_settings(db: Session) -> AppSettings:
    """Возвращает единственную строку настроек, создаёт при отсутствии."""
    row = db.get(AppSettings, 1)
    if row is not None:
        return row
    row = AppSettings(id=1, wallet_buttons_enabled=False, debit_code_enabled=False)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def wallet_buttons_enabled(db: Session) -> bool:
    """Глобально включены ли кнопки добавления в кошелёк."""
    return bool(get_app_settings(db).wallet_buttons_enabled)


def set_wallet_buttons_enabled(db: Session, enabled: bool) -> AppSettings:
    """Включает или выключает глобальный показ кнопок кошелька."""
    row = get_app_settings(db)
    row.wallet_buttons_enabled = enabled
    db.commit()
    db.refresh(row)
    return row


def certificate_shows_wallet_buttons(db: Session, certificate: Certificate) -> bool:
    """Кнопки кошелька: глобально или флаг тестовой карты."""
    if getattr(certificate, "show_wallet_buttons", False):
        return True
    return wallet_buttons_enabled(db)


def wallet_enabled(db: Session) -> bool:
    """Глобальный выключатель WalletWallet."""
    return bool(get_app_settings(db).wallet_enabled)


def set_wallet_enabled(db: Session, enabled: bool) -> AppSettings:
    """Включает или выключает пилот Apple Wallet."""
    row = get_app_settings(db)
    row.wallet_enabled = enabled
    db.commit()
    db.refresh(row)
    return row


def wallet_show_balance(db: Session) -> bool:
    """Показывать баланс в headerFields pass."""
    return bool(get_app_settings(db).wallet_show_balance)


def wallet_notify_on_change(db: Session) -> bool:
    """changeMessage на поле баланса."""
    return bool(get_app_settings(db).wallet_notify_on_change)


def set_wallet_balance_settings(
    db: Session,
    *,
    show_balance: bool,
    notify_on_change: bool,
) -> AppSettings:
    """Настройки баланса на pass."""
    row = get_app_settings(db)
    row.wallet_show_balance = show_balance
    row.wallet_notify_on_change = notify_on_change
    db.commit()
    db.refresh(row)
    return row


def wallet_feature_enabled(db: Session) -> bool:
    """Можно ли выпускать pass (флаг и нет паузы после 429)."""
    row = get_app_settings(db)
    if not row.wallet_enabled:
        return False
    until = row.wallet_provider_paused_until
    if until is None:
        return True
    now = datetime.now(timezone.utc)
    aware = until if until.tzinfo is not None else until.replace(tzinfo=timezone.utc)
    return aware <= now


def pause_wallet_provider(db: Session, *, hours: int = 1) -> AppSettings:
    """Временно отключает выдачу после 429 WalletWallet."""
    row = get_app_settings(db)
    row.wallet_provider_paused_until = datetime.now(timezone.utc) + timedelta(hours=max(1, hours))
    db.commit()
    db.refresh(row)
    return row


def certificate_wallet_allowed(certificate: Certificate) -> bool:
    """Флаг доступа к Wallet на карте."""
    return bool(getattr(certificate, "wallet_allowed", False))


def update_debit_code_settings(
    db: Session,
    *,
    enabled: bool,
    scope: str,
    min_amount: int,
    ttl_sec: int,
    max_attempts: int,
    on_failure: str,
) -> AppSettings:
    """Сохраняет настройки подтверждения списания кодом."""
    row = get_app_settings(db)
    row.debit_code_enabled = enabled
    row.debit_code_scope = scope if scope in {"all", "marked"} else "marked"
    row.debit_code_min_amount = max(0, min_amount)
    row.debit_code_ttl_sec = max(30, ttl_sec)
    row.debit_code_max_attempts = max(1, max_attempts)
    row.debit_code_on_failure = on_failure if on_failure in {"deny", "allow"} else "deny"
    db.commit()
    db.refresh(row)
    return row

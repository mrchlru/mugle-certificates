"""Глобальные настройки приложения из таблицы app_settings."""

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

"""Глобальные настройки приложения из таблицы app_settings."""

from sqlalchemy.orm import Session

from bonus.models import AppSettings, Certificate


def get_app_settings(db: Session) -> AppSettings:
    """Возвращает единственную строку настроек, создаёт при отсутствии."""
    row = db.get(AppSettings, 1)
    if row is not None:
        return row
    row = AppSettings(id=1, wallet_buttons_enabled=False)
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

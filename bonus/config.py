"""Настройки приложения из переменных окружения."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Параметры запуска. Секреты приходят из окружения, не из кода."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "dev-change-me"
    database_url: str = "sqlite:///./data/bonus.db"
    data_dir: str = "./data"
    app_public_url: str = ""
    https_only: bool = False
    seed_demo: bool = True
    hr_login: str = "hr"
    hr_password: str = "mugle-hr"
    hr_name: str = "HR Mugle Rest"
    manager_login: str = "manager"
    manager_password: str = "mugle-desk"
    manager_name: str = "Управляющий"
    manager_restaurant: str = "Хачапури Марико"
    s3_endpoint_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = ""
    s3_region: str = "ru-1"
    s3_public_base_url: str = ""
    s3_prefix: str = "templates"

    def normalized_database_url(self) -> str:
        """Возвращает URL базы в формате SQLAlchemy."""
        return _normalize_database_url(self.database_url)

    def data_path(self) -> Path:
        """Каталог файла SQLite и одноразовых учётных данных."""
        return Path(self.data_dir)

    def uses_default_secret(self) -> bool:
        """Показывает, что секрет подписи сессий ещё не заменили."""
        return self.secret_key == "dev-change-me"


@lru_cache
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек."""
    return Settings()


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url

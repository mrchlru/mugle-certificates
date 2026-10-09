"""Подключение к SQLite или PostgreSQL."""

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from bonus.models import Base
from bonus.schema import ensure_schema

engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def configure_database(url: str) -> None:
    """Создаёт движок и таблицы. Повторный вызов подменяет подключение."""
    global engine, SessionLocal
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    _install_sqlite_pragmas(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    ensure_schema(engine)


def open_session() -> Session:
    """Открывает сессию уже после настройки базы."""
    if SessionLocal is None:
        raise RuntimeError("База ещё не настроена")
    return SessionLocal()


def get_db() -> Iterator[Session]:
    """Отдаёт сессию на время одного запроса."""
    db = open_session()
    try:
        yield db
    finally:
        db.close()


def _install_sqlite_pragmas(bound: Engine) -> None:
    if not str(bound.url).startswith("sqlite"):
        return

    @event.listens_for(bound, "connect")
    def _pragmas(connection: object, _record: object) -> None:
        cursor = connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        cursor.close()

"""Дописывает колонки в уже существующую базу без Alembic."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def ensure_schema(engine: Engine) -> None:
    """Добавляет новые поля сертификата, если база создана раньше."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "certificates" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("certificates")}
    statements: list[str] = []
    if "segment_id" not in columns:
        statements.append("ALTER TABLE certificates ADD COLUMN segment_id INTEGER")
    if "template_id" not in columns:
        statements.append("ALTER TABLE certificates ADD COLUMN template_id INTEGER")
    if "color" not in columns:
        statements.append("ALTER TABLE certificates ADD COLUMN color VARCHAR(7) DEFAULT '#00A8A8'")
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

"""Дописывает колонки в уже существующую базу без Alembic."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def ensure_schema(engine: Engine) -> None:
    """Добавляет новые поля, если база создана раньше."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    statements: list[str] = []
    if "certificates" in tables:
        columns = {column["name"] for column in inspector.get_columns("certificates")}
        if "segment_id" not in columns:
            statements.append("ALTER TABLE certificates ADD COLUMN segment_id INTEGER")
        if "template_id" not in columns:
            statements.append("ALTER TABLE certificates ADD COLUMN template_id INTEGER")
        if "color" not in columns:
            statements.append("ALTER TABLE certificates ADD COLUMN color VARCHAR(7) DEFAULT '#00A8A8'")
        if "discount_percent" not in columns:
            statements.append("ALTER TABLE certificates ADD COLUMN discount_percent INTEGER DEFAULT 0")
    if "card_templates" in tables:
        columns = {column["name"] for column in inspector.get_columns("card_templates")}
        if "discount_percent" not in columns:
            statements.append(
                "ALTER TABLE card_templates ADD COLUMN discount_percent INTEGER DEFAULT 0"
            )
    if "transactions" in tables:
        columns = {column["name"] for column in inspector.get_columns("transactions")}
        if "restaurant" not in columns:
            statements.append("ALTER TABLE transactions ADD COLUMN restaurant VARCHAR(200) DEFAULT ''")
    if "users" in tables:
        columns = {column["name"] for column in inspector.get_columns("users")}
        if "department" not in columns:
            statements.append("ALTER TABLE users ADD COLUMN department VARCHAR(120) DEFAULT ''")
        if "job_title" not in columns:
            statements.append("ALTER TABLE users ADD COLUMN job_title VARCHAR(120) DEFAULT ''")
        if "phone" not in columns:
            statements.append("ALTER TABLE users ADD COLUMN phone VARCHAR(40) DEFAULT ''")
    if "segments" in tables:
        columns = {column["name"] for column in inspector.get_columns("segments")}
        if "accrual_period" not in columns:
            statements.append(
                "ALTER TABLE segments ADD COLUMN accrual_period VARCHAR(16) DEFAULT 'none'"
            )
        if "requires_expiry" not in columns:
            statements.append(
                "ALTER TABLE segments ADD COLUMN requires_expiry BOOLEAN DEFAULT FALSE"
            )
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

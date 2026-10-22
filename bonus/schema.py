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
        if "single_use" not in columns:
            statements.append("ALTER TABLE certificates ADD COLUMN single_use BOOLEAN DEFAULT FALSE")
        if "ledger_sheet" not in columns:
            statements.append(
                "ALTER TABLE certificates ADD COLUMN ledger_sheet VARCHAR(32) DEFAULT 'employee'"
            )
        if "company" not in columns:
            statements.append("ALTER TABLE certificates ADD COLUMN company VARCHAR(200) DEFAULT ''")
        if "birth_date" not in columns:
            statements.append("ALTER TABLE certificates ADD COLUMN birth_date DATE")
        if "use_days" not in columns:
            statements.append(
                "ALTER TABLE certificates ADD COLUMN use_days VARCHAR(16) DEFAULT '1234567'"
            )
        if "accrual_days" not in columns:
            statements.append(
                "ALTER TABLE certificates ADD COLUMN accrual_days VARCHAR(16) DEFAULT '12345'"
            )
        if "gift_opened_at" not in columns:
            statements.append("ALTER TABLE certificates ADD COLUMN gift_opened_at TIMESTAMP")
        if "unwrap_pending" not in columns:
            statements.append(
                "ALTER TABLE certificates ADD COLUMN unwrap_pending BOOLEAN DEFAULT FALSE"
            )
        if "show_wallet_buttons" not in columns:
            statements.append(
                "ALTER TABLE certificates ADD COLUMN show_wallet_buttons BOOLEAN DEFAULT FALSE"
            )
    if "app_settings" not in tables:
        statements.append(
            """
            CREATE TABLE app_settings (
                id INTEGER PRIMARY KEY,
                wallet_buttons_enabled BOOLEAN DEFAULT FALSE
            )
            """
        )
    if "card_templates" in tables:
        columns = {column["name"] for column in inspector.get_columns("card_templates")}
        if "discount_percent" not in columns:
            statements.append(
                "ALTER TABLE card_templates ADD COLUMN discount_percent INTEGER DEFAULT 0"
            )
        if "logo_path" not in columns:
            statements.append(
                "ALTER TABLE card_templates ADD COLUMN logo_path VARCHAR(200) DEFAULT ''"
            )
        if "color" not in columns:
            statements.append(
                "ALTER TABLE card_templates ADD COLUMN color VARCHAR(7) DEFAULT '#00A8A8'"
            )
        if "segment_id" not in columns:
            statements.append("ALTER TABLE card_templates ADD COLUMN segment_id INTEGER")
    if "transactions" in tables:
        columns = {column["name"] for column in inspector.get_columns("transactions")}
        if "restaurant" not in columns:
            statements.append("ALTER TABLE transactions ADD COLUMN restaurant VARCHAR(200) DEFAULT ''")
        if "order_ref" not in columns:
            statements.append("ALTER TABLE transactions ADD COLUMN order_ref VARCHAR(80) DEFAULT ''")
        if "cheque_detail" not in columns:
            statements.append("ALTER TABLE transactions ADD COLUMN cheque_detail TEXT DEFAULT ''")
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
        if "discount_percent" not in columns:
            statements.append(
                "ALTER TABLE segments ADD COLUMN discount_percent INTEGER DEFAULT 0"
            )
        if "single_use" not in columns:
            statements.append("ALTER TABLE segments ADD COLUMN single_use BOOLEAN DEFAULT FALSE")
        if "use_days" not in columns:
            statements.append(
                "ALTER TABLE segments ADD COLUMN use_days VARCHAR(16) DEFAULT '1234567'"
            )
        if "accrual_days" not in columns:
            statements.append(
                "ALTER TABLE segments ADD COLUMN accrual_days VARCHAR(16) DEFAULT '12345'"
            )
        if "birthday_unwrap" not in columns:
            statements.append(
                "ALTER TABLE segments ADD COLUMN birthday_unwrap BOOLEAN DEFAULT FALSE"
            )
        if "unwrap_kind" not in columns:
            statements.append(
                "ALTER TABLE segments ADD COLUMN unwrap_kind VARCHAR(16) DEFAULT ''"
            )
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    _migrate_unwrap_kind(engine)
    _backfill_unwrap_pending(engine)
    _ensure_app_settings_row(engine)


def _ensure_app_settings_row(engine: Engine) -> None:
    """Гарантирует строку id=1 в app_settings."""
    inspector = inspect(engine)
    if "app_settings" not in inspector.get_table_names():
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_settings (id, wallet_buttons_enabled)
                SELECT 1, FALSE
                WHERE NOT EXISTS (SELECT 1 FROM app_settings WHERE id = 1)
                """
            )
        )


def _migrate_unwrap_kind(engine: Engine) -> None:
    """Переносит старый birthday_unwrap → unwrap_kind='birthday'."""
    inspector = inspect(engine)
    if "segments" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("segments")}
    if "unwrap_kind" not in columns:
        return
    with engine.begin() as connection:
        if "birthday_unwrap" in columns:
            connection.execute(
                text(
                    """
                    UPDATE segments
                    SET unwrap_kind = 'birthday'
                    WHERE birthday_unwrap IS TRUE
                      AND (unwrap_kind IS NULL OR unwrap_kind = '')
                    """
                )
            )


def _backfill_unwrap_pending(engine: Engine) -> None:
    """Проставляет unwrap_pending картам сегментов с распаковкой, ещё не открытым."""
    inspector = inspect(engine)
    if "certificates" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("certificates")}
    if "unwrap_pending" not in columns:
        return
    segment_columns = (
        {column["name"] for column in inspector.get_columns("segments")}
        if "segments" in inspector.get_table_names()
        else set()
    )
    with engine.begin() as connection:
        if "unwrap_kind" in segment_columns:
            connection.execute(
                text(
                    """
                    UPDATE certificates
                    SET unwrap_pending = TRUE
                    WHERE unwrap_pending IS NOT TRUE
                      AND gift_opened_at IS NULL
                      AND segment_id IN (
                        SELECT id FROM segments
                        WHERE unwrap_kind IN ('birthday', 'gift')
                      )
                    """
                )
            )
        elif "birthday_unwrap" in segment_columns:
            connection.execute(
                text(
                    """
                    UPDATE certificates
                    SET unwrap_pending = TRUE
                    WHERE unwrap_pending IS NOT TRUE
                      AND gift_opened_at IS NULL
                      AND segment_id IN (
                        SELECT id FROM segments WHERE birthday_unwrap IS TRUE
                      )
                    """
                )
            )
        connection.execute(
            text(
                """
                UPDATE certificates
                SET unwrap_pending = FALSE
                WHERE gift_opened_at IS NOT NULL AND unwrap_pending IS TRUE
                """
            )
        )

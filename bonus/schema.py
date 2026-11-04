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
        if "is_deleted" not in columns:
            statements.append(
                "ALTER TABLE certificates ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE"
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
    if "link_deliveries" not in tables:
        statements.append(
            """
            CREATE TABLE link_deliveries (
                id INTEGER PRIMARY KEY,
                certificate_id INTEGER NOT NULL UNIQUE,
                group_label VARCHAR(200) DEFAULT '',
                marked_by_id INTEGER,
                marked_at TIMESTAMP
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
        if "segment2_id" not in columns:
            statements.append("ALTER TABLE card_templates ADD COLUMN segment2_id INTEGER")
        if "is_deleted" not in columns:
            statements.append(
                "ALTER TABLE card_templates ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE"
            )
    if "certificate_parts" not in tables:
        statements.append(
            """
            CREATE TABLE certificate_parts (
                id INTEGER PRIMARY KEY,
                certificate_id INTEGER NOT NULL,
                segment_id INTEGER NOT NULL,
                sort_order INTEGER DEFAULT 1,
                segment_name VARCHAR(120) DEFAULT '',
                accrual_period VARCHAR(16) DEFAULT 'none',
                use_days VARCHAR(16) DEFAULT '1234567',
                accrual_days VARCHAR(16) DEFAULT '12345',
                nominal INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                created_at TIMESTAMP,
                CONSTRAINT uq_cert_part_segment UNIQUE (certificate_id, segment_id)
            )
            """
        )
    if "transactions" in tables:
        columns = {column["name"] for column in inspector.get_columns("transactions")}
        if "restaurant" not in columns:
            statements.append("ALTER TABLE transactions ADD COLUMN restaurant VARCHAR(200) DEFAULT ''")
        if "order_ref" not in columns:
            statements.append("ALTER TABLE transactions ADD COLUMN order_ref VARCHAR(80) DEFAULT ''")
        if "cheque_detail" not in columns:
            statements.append("ALTER TABLE transactions ADD COLUMN cheque_detail TEXT DEFAULT ''")
        if "part_id" not in columns:
            statements.append("ALTER TABLE transactions ADD COLUMN part_id INTEGER")
    if "users" in tables:
        columns = {column["name"] for column in inspector.get_columns("users")}
        if "department" not in columns:
            statements.append("ALTER TABLE users ADD COLUMN department VARCHAR(120) DEFAULT ''")
        if "job_title" not in columns:
            statements.append("ALTER TABLE users ADD COLUMN job_title VARCHAR(120) DEFAULT ''")
        if "phone" not in columns:
            statements.append("ALTER TABLE users ADD COLUMN phone VARCHAR(40) DEFAULT ''")
        if "spasibo_user_id" not in columns:
            statements.append(
                "ALTER TABLE users ADD COLUMN spasibo_user_id VARCHAR(32) DEFAULT ''"
            )
        if "spasibo_certificate_id" not in columns:
            statements.append("ALTER TABLE users ADD COLUMN spasibo_certificate_id INTEGER")
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
        if "is_deleted" not in columns:
            statements.append(
                "ALTER TABLE segments ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE"
            )
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    _migrate_unwrap_kind(engine)
    _backfill_unwrap_pending(engine)
    _backfill_certificate_parts(engine)
    _ensure_app_settings_row(engine)
    _ensure_summary_indexes(engine)
    _ensure_history_indexes(engine)


def _ensure_history_indexes(engine: Engine) -> None:
    """Индекс для постраничной истории операций по карте."""
    inspector = inspect(engine)
    if "transactions" not in inspector.get_table_names():
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_transactions_certificate_id_id "
                "ON transactions (certificate_id, id)"
            )
        )


def _ensure_summary_indexes(engine: Engine) -> None:
    """Индексы для быстрой сводки HR (фильтр сегмента / срок)."""
    inspector = inspect(engine)
    if "certificates" not in inspector.get_table_names():
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_certificates_deleted_segment "
                "ON certificates (is_deleted, segment_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_certificates_expires_on "
                "ON certificates (expires_on)"
            )
        )
    dialect = (engine.dialect.name or "").casefold()
    if dialect != "postgresql":
        return
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_users_full_name_trgm "
                    "ON users USING gin (lower(full_name) gin_trgm_ops)"
                )
            )
    except Exception:
        # Нет прав на extension / GIN — сводка всё равно работает без них.
        pass


def _backfill_certificate_parts(engine: Engine) -> dict[str, int]:
    """Создаёт одну часть на каждую существующую карту без частей.

    Возвращает сверку: карт, созданных частей, расхождений суммы.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "certificates" not in tables or "certificate_parts" not in tables:
        return {"cards": 0, "created": 0, "mismatch": 0}
    with engine.begin() as connection:
        created = connection.execute(
            text(
                """
                INSERT INTO certificate_parts (
                    certificate_id, segment_id, sort_order, segment_name,
                    accrual_period, use_days, accrual_days, nominal, balance, created_at
                )
                SELECT
                    c.id,
                    c.segment_id,
                    1,
                    COALESCE(s.name, ''),
                    COALESCE(s.accrual_period, 'none'),
                    COALESCE(NULLIF(c.use_days, ''), '1234567'),
                    COALESCE(NULLIF(c.accrual_days, ''), '12345'),
                    COALESCE(c.nominal, 0),
                    COALESCE(c.balance, 0),
                    CURRENT_TIMESTAMP
                FROM certificates c
                LEFT JOIN segments s ON s.id = c.segment_id
                WHERE c.segment_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM certificate_parts p
                    WHERE p.certificate_id = c.id
                  )
                """
            )
        ).rowcount
        mismatch = connection.execute(
            text(
                """
                SELECT COUNT(*) FROM (
                    SELECT c.id
                    FROM certificates c
                    JOIN certificate_parts p ON p.certificate_id = c.id
                    GROUP BY c.id, c.balance
                    HAVING COALESCE(SUM(p.balance), 0) != COALESCE(c.balance, 0)
                ) bad
                """
            )
        ).scalar() or 0
        cards = connection.execute(
            text("SELECT COUNT(*) FROM certificates WHERE segment_id IS NOT NULL")
        ).scalar() or 0
    result = {
        "cards": int(cards),
        "created": int(created or 0),
        "mismatch": int(mismatch),
    }
    print(
        f"Teams Bonus: certificate_parts — карт {result['cards']}, "
        f"создано частей {result['created']}, расхождений {result['mismatch']}",
        flush=True,
    )
    return result


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

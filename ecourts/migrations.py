"""
ecourts.migrations

Non-destructive schema additions needed for ecourts sync foundation.
Run apply_ecourts_migrations_if_needed() under app.app_context() when you
want to add the required columns/tables. This helper is idempotent and
uses a SQLite-friendly sequence for adding the `hearing.source` default
value (add nullable column, update rows, avoid adding NOT NULL constraints).
"""
from sqlalchemy import text


def apply_ecourts_migrations_if_needed():
    # import db lazily to avoid circular imports at module import time
    from app import db

    inspector = db.inspect(db.engine)
    table_names = inspector.get_table_names()

    # --- case table additions
    existing_case_cols = set()
    if "case" in table_names:
        existing_case_cols = {c["name"] for c in inspector.get_columns("case")}
    case_columns_to_add = {
        "last_synced_at": "TIMESTAMP",
        "sync_status": "VARCHAR(50)",
        "last_sync_error": "TEXT",
        "ecourts_id": "VARCHAR(255)"
    }
    for col, col_type in case_columns_to_add.items():
        if col not in existing_case_cols:
            db.session.execute(text(f'ALTER TABLE "case" ADD COLUMN {col} {col_type}'))
    db.session.commit()

    # --- hearing table additions (SQLite-safe)
    existing_hearing_cols = set()
    if "hearing" in table_names:
        existing_hearing_cols = {c["name"] for c in inspector.get_columns("hearing")}
    # We will add `source` as a nullable column first, then set existing rows to 'user'
    hearing_columns_to_add = {
        # add nullable first; do not force NOT NULL in a single ALTER for SQLite compatibility
        "source": "VARCHAR(50)",
        "source_id": "VARCHAR(255)",
        "synced_at": "TIMESTAMP"
    }
    for col, col_type in hearing_columns_to_add.items():
        if col not in existing_hearing_cols:
            db.session.execute(text(f'ALTER TABLE "hearing" ADD COLUMN {col} {col_type}'))
    db.session.commit()

    # If we added source (or it was present but contains NULLs), set defaults for existing rows
    if "hearing" in table_names:
        try:
            db.session.execute(text("UPDATE hearing SET source = 'user' WHERE source IS NULL"))
            db.session.commit()
        except Exception:
            # Swallow any error here as it's best-effort; caller can inspect the DB if needed.
            db.session.rollback()

    # --- sync_log table
    if "sync_log" not in table_names:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY,
                case_id INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                success BOOLEAN,
                old_next_hearing_date DATE,
                new_next_hearing_date DATE,
                source VARCHAR(50),
                raw_payload TEXT,
                error_message TEXT,
                FOREIGN KEY(case_id) REFERENCES "case"(id)
            )
        """))
        db.session.commit()

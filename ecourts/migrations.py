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
        "ecourts_id": "VARCHAR(255)",
        "normalized_crn": "VARCHAR(200)"
    }
    for col, col_type in case_columns_to_add.items():
        if col not in existing_case_cols:
            db.session.execute(text(f'ALTER TABLE "case" ADD COLUMN {col} {col_type}'))
    db.session.commit()

    # backfill normalized_crn from existing crn_no where present
    if "case" in table_names:
        try:
            db.session.execute(text(
                'UPDATE "case" SET normalized_crn = lower(trim(crn_no)) WHERE crn_no IS NOT NULL AND (normalized_crn IS NULL OR normalized_crn = "")'
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Create index on normalized_crn if not exists (safe)
    try:
        db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_case_normalized_crn ON "case"(normalized_crn)'))
        db.session.commit()
    except Exception:
        db.session.rollback()

    # --- hearing table additions (SQLite-safe)
    existing_hearing_cols = set()
    if "hearing" in table_names:
        existing_hearing_cols = {c["name"] for c in inspector.get_columns("hearing")}
    hearing_columns_to_add = {
        "source": "VARCHAR(50)",
        "source_id": "VARCHAR(255)",
        "synced_at": "TIMESTAMP",
        "outcome_normalized": "VARCHAR(300)"
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
            db.session.rollback()

    # backfill outcome_normalized from existing outcome
    if "hearing" in table_names:
        try:
            db.session.execute(text(
                'UPDATE hearing SET outcome_normalized = lower(trim(outcome)) WHERE outcome IS NOT NULL AND (outcome_normalized IS NULL OR outcome_normalized = "")'
            ))
            db.session.commit()
        except Exception:
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

    # --- indexes for duplicate detection (non-unique by default)
    try:
        db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_hearing_case_source_sourceid ON hearing(case_id, source, source_id)'))
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_hearing_case_source_date_outcome ON hearing(case_id, source, hearing_date, outcome_normalized)'))
        db.session.commit()
    except Exception:
        db.session.rollback()

    # --- attempt to create unique indexes only if data is clean

    # unique on (case_id, source, source_id) where source_id is not null
    try:
        dup_check = db.session.execute(text("""
            SELECT case_id, source, source_id, COUNT(*) AS cnt
            FROM hearing
            WHERE source_id IS NOT NULL
            GROUP BY case_id, source, source_id
            HAVING cnt > 1
            LIMIT 1
        """
        )).fetchone()
        if dup_check is None:
            try:
                db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_hearing_case_source_sourceid ON hearing(case_id, source, source_id)'))
                db.session.commit()
            except Exception:
                db.session.rollback()
    except Exception:
        db.session.rollback()

    # unique on (case_id, source, hearing_date, outcome_normalized) where outcome_normalized IS NOT NULL
    try:
        dup_check2 = db.session.execute(text("""
            SELECT case_id, source, hearing_date, outcome_normalized, COUNT(*) AS cnt
            FROM hearing
            WHERE outcome_normalized IS NOT NULL
            GROUP BY case_id, source, hearing_date, outcome_normalized
            HAVING cnt > 1
            LIMIT 1
        """
        )).fetchone()
        if dup_check2 is None:
            try:
                db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_hearing_case_source_date_outcome ON hearing(case_id, source, hearing_date, outcome_normalized)'))
                db.session.commit()
            except Exception:
                db.session.rollback()
    except Exception:
        db.session.rollback()

#!/usr/bin/env python3
"""
Read-only diagnostic for Court Tracker SQLite DB.

Purpose:
- Identify DB file used (by default same logic as app.py)
- Report Case row count
- Report Hearing row count
- Report whether the current Hearing table has:
   source, source_id, synced_at
- Report whether the current Case table has:
   crn_no, last_synced_at, sync_status, last_sync_error, ecourts_id
- Detect duplicate CNRs using trim + casefold normalization
- If hearing.source/source_id exist, detect possible duplicate ecourts hearing groups:
    * duplicates by (case_id, source, source_id)
    * duplicates by (case_id, source, hearing_date, normalized outcome)
- Count NULL/empty CNR values
- Count NULL Hearing source values (if column exists)
- Produce a human-readable report

IMPORTANT:
- READ ONLY: uses sqlite3 in read-only mode (URI mode=ro)
- No schema or data modifications
- Intended to be run locally against the DB your app uses (or a copy)
"""
import os
import sqlite3
import sys
from collections import defaultdict

# ---------------------------
# Configuration & utilities
# ---------------------------

DEFAULT_SQLITE_URI = "sqlite:///court_tracker.db"


def get_database_uri():
    """
    Determine the DB URI using the same precedence as app.py:
    - If env DATABASE_URL is set, use it
    - Else use default sqlite:///court_tracker.db
    Return the raw DATABASE_URL string.
    """
    return os.environ.get("DATABASE_URL", DEFAULT_SQLITE_URI)


def parse_sqlite_path(db_uri: str):
    """
    Parse a DATABASE_URL style URI for SQLite and return filesystem path.
    Supports:
      - sqlite:///relative_or_absolute_path
      - sqlite:////absolute_path (rare)
      - sqlite:///:memory:  (special)
      - file:path?mode=ro URIs are not parsed here (we expect DATABASE_URL style).
    Returns (path, is_memory)
    """
    if not db_uri:
        return None, False
    if db_uri.startswith("sqlite://"):
        # strip prefix
        path_part = db_uri[len("sqlite://"):]
        # Common case: sqlite:///path -> path_part starts with /path
        if path_part == ":memory:" or path_part.startswith(":memory:"):
            return ":memory:", True
        # Remove a single leading slash if sqlite:///relative_path was intended
        # But app.py uses sqlite:///court_tracker.db which yields '/court_tracker.db' in path_part.
        # For our purposes we will treat path_part as filesystem path as-is (usually correct).
        # If path_part is empty, return default file.
        if not path_part:
            return "court_tracker.db", False
        # On many setups path_part will begin with /<path>; remove leading slash only if it looks like relative with triple slash?
        # Keep path_part as-is but clean leading slashes for Windows? We'll simply strip a single leading slash if present and user desires relative.
        # Simpler: if path_part.startswith("/"):
        possible_path = path_part
        # If path begins with three slashes scenario (sqlite:///relative), path_part starts with /relative; we want 'relative'
        if possible_path.startswith("/") and not possible_path.startswith("//"):
            # Remove single leading slash to get the path used by SQLALCHEMY (which uses sqlite:///file)
            return possible_path.lstrip("/"), False
        return possible_path, False
    elif db_uri.startswith("sqlite:"):
        # e.g., sqlite:/absolute/path or sqlite:relative.db
        return db_uri[len("sqlite:"):], False
    else:
        return None, False


def open_sqlite_ro(path):
    """
    Open SQLite DB in read-only mode using URI parameter mode=ro.
    Returns sqlite3.Connection
    """
    # For in-memory path, we cannot open in read-only; return None
    if path == ":memory:":
        return None
    # Build URI for read-only open
    # If path contains query params already, keep them out; we'll use file:... format
    # Using sqlite URI: file:ABS_PATH?mode=ro
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        print(f"ERROR: Unable to open database in read-only mode at '{path}': {e}", file=sys.stderr)
        return None


def table_exists(conn, table_name):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE", (table_name,)
    )
    return cur.fetchone() is not None


def pragma_table_info(conn, table_name):
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    rows = cur.fetchall()
    # rows have columns: cid, name, type, notnull, dflt_value, pk
    return [dict(r) for r in rows]


def count_rows(conn, table_name):
    cur = conn.execute(f"SELECT COUNT(*) as cnt FROM \"{table_name}\"")
    return cur.fetchone()["cnt"]


def fetch_iter(conn, sql, params=()):
    cur = conn.execute(sql, params)
    for row in cur:
        yield row


# Normalization helpers (must match service/normalizer behavior)
def normalize_crn_for_index(crn):
    """
    The plan calls for trim + casefold normalization for CNR.
    We implement that here.
    """
    if crn is None:
        return None
    s = str(crn).strip()
    if s == "":
        return None
    return s.casefold()


def normalize_outcome_for_index(outcome):
    """
    Use the same normalization as SyncService._normalize_text:
      - lower-case
      - collapse whitespace sequences into single spaces
    """
    if outcome is None:
        return ""
    return " ".join(str(outcome).lower().split())


# ---------------------------
# Diagnostic logic
# ---------------------------

def inspect_db():
    report_lines = []
    db_uri = get_database_uri()
    report_lines.append(f"Detected DATABASE_URL (from environment or default): {db_uri}")

    sqlite_path, is_memory = parse_sqlite_path(db_uri)
    if sqlite_path is None:
        report_lines.append("ERROR: DATABASE_URL does not appear to be a SQLite URI. This script only handles SQLite.")
        return "\n".join(report_lines)

    if is_memory:
        report_lines.append("The DATABASE_URL points to an in-memory SQLite database (:memory:).")
        report_lines.append("Cannot inspect an in-memory DB from this external script unless it is already running in the same process.")
        return "\n".join(report_lines)

    report_lines.append(f"Interpreted SQLite file path: {sqlite_path}")
    if not os.path.exists(sqlite_path):
        report_lines.append(f"WARNING: SQLite file '{sqlite_path}' does not exist at this path in the current working directory.")
        report_lines.append("If your app runs elsewhere or sets DATABASE_URL differently, inspect that environment.")
        # We will still attempt to open; open_sqlite_ro will likely error.
    conn = open_sqlite_ro(sqlite_path)
    if conn is None:
        report_lines.append("ERROR: Could not open the SQLite DB in read-only mode. Aborting further checks.")
        return "\n".join(report_lines)

    # Basic table existence
    has_case = table_exists(conn, "case")
    has_hearing = table_exists(conn, "hearing")
    report_lines.append(f"Table presence: case: {has_case}, hearing: {has_hearing}")

    if not has_case or not has_hearing:
        report_lines.append("ERROR: Required tables (case and/or hearing) are not present in the DB. Inspect schema or app configuration.")
        conn.close()
        return "\n".join(report_lines)

    # Row counts
    try:
        case_count = count_rows(conn, "case")
    except Exception as e:
        report_lines.append(f"ERROR counting case rows: {e}")
        case_count = None
    try:
        hearing_count = count_rows(conn, "hearing")
    except Exception as e:
        report_lines.append(f"ERROR counting hearing rows: {e}")
        hearing_count = None

    report_lines.append(f"Case row count: {case_count}")
    report_lines.append(f"Hearing row count: {hearing_count}")

    # Inspect columns
    case_info = pragma_table_info(conn, "case")
    hearing_info = pragma_table_info(conn, "hearing")
    case_cols = {c["name"] for c in case_info}
    hearing_cols = {c["name"] for c in hearing_info}
    report_lines.append(f"Case table columns ({len(case_cols)}): {sorted(case_cols)}")
    report_lines.append(f"Hearing table columns ({len(hearing_cols)}): {sorted(hearing_cols)}")

    # Check required columns presence
    required_case_cols = ["crn_no", "last_synced_at", "sync_status", "last_sync_error", "ecourts_id"]
    required_hearing_cols = ["source", "source_id", "synced_at"]

    for c in required_case_cols:
        report_lines.append(f"Case has column '{c}': {c in case_cols}")
    for h in required_hearing_cols:
        report_lines.append(f"Hearing has column '{h}': {h in hearing_cols}")

    # Count NULL/empty crn_no values
    try:
        cur = conn.execute("SELECT COUNT(*) AS cnt FROM \"case\" WHERE crn_no IS NULL OR TRIM(crn_no) = ''")
        null_crn_count = cur.fetchone()["cnt"]
        report_lines.append(f"Case rows with NULL or empty crn_no: {null_crn_count}")
    except Exception as e:
        report_lines.append(f"Could not count NULL/empty crn_no: {e}")
        null_crn_count = None

    # Detect duplicate CNRs using trim + casefold (do in Python to match casefold)
    report_lines.append("Detecting duplicate CNRs using trim + casefold normalization (Python):")
    dup_crn_groups = {}
    try:
        cur = conn.execute("SELECT id, crn_no FROM \"case\"")
        groups = {}
        for row in cur:
            cid = row["id"]
            crn = row["crn_no"]
            norm = normalize_crn_for_index(crn)
            if norm is None:
                continue
            groups.setdefault(norm, []).append(cid)
        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
        if dup_groups:
            report_lines.append(f"Found {len(dup_groups)} normalized CNR groups with duplicates:")
            for norm, ids in dup_groups.items():
                report_lines.append(f"  normalized='{norm}' -> case ids: {ids}")
        else:
            report_lines.append("No duplicate normalized CNR groups found.")
    except Exception as e:
        report_lines.append(f"Error while detecting duplicate CNRs: {e}")

    # Hearing duplicate detections 
# only if hearing.source and hearing.source_id exist
    if {"source", "source_id"}.issubset(hearing_cols):
        report_lines.append("Hearing provenance columns present; detecting duplicates by (case_id, source, source_id):")
        try:
            cur = conn.execute(
                "SELECT case_id, source, source_id, COUNT(*) AS cnt "
                "FROM hearing "
                "WHERE source_id IS NOT NULL "
                "GROUP BY case_id, source, source_id "
                "HAVING COUNT(*) > 1"
            )
            rows = cur.fetchall()
            if rows:
                report_lines.append(f"Found {len(rows)} groups where (case_id, source, source_id) is duplicated:")
                for r in rows:
                    report_lines.append(f"  case_id={r['case_id']} source={r['source']} source_id={r['source_id']} count={r['cnt']}")
            else:
                report_lines.append("No (case_id, source, source_id) duplicates found.")
        except Exception as e:
            report_lines.append(f"Error checking (case_id, source, source_id) duplicates: {e}")

        # Count NULL source values
        try:
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM hearing WHERE source IS NULL")
            cnt_null_source = cur.fetchone()["cnt"]
            report_lines.append(f"Hearing rows with NULL source: {cnt_null_source}")
        except Exception as e:
            report_lines.append(f"Could not count NULL source values: {e}")

        # Duplicate detection by (case_id, source, hearing_date, normalized outcome)
        report_lines.append("Detecting duplicates by (case_id, source, hearing_date, normalized outcome):")
        # If an outcome_normalized column exists, we can use it directly; otherwise compute normalized outcome in Python
        if "outcome_normalized" in hearing_cols:
            try:
                cur = conn.execute(
                    "SELECT case_id, source, hearing_date, outcome_normalized, COUNT(*) AS cnt "
                    "FROM hearing "
                    "WHERE outcome_normalized IS NOT NULL AND hearing_date IS NOT NULL "
                    "GROUP BY case_id, source, hearing_date, outcome_normalized "
                    "HAVING COUNT(*) > 1"
                )
                rows = cur.fetchall()
                if rows:
                    report_lines.append(f"Found {len(rows)} duplicate groups by precomputed outcome_normalized:")
                    for r in rows:
                        report_lines.append(f"  case_id={r['case_id']} source={r['source']} hearing_date={r['hearing_date']} outcome_normalized='{r['outcome_normalized']}' count={r['cnt']}")
                else:
                    report_lines.append("No duplicates found using outcome_normalized column.")
            except Exception as e:
                report_lines.append(f"Error checking outcome_normalized duplicates: {e}")
        else:
            # Compute normalized outcome in Python and group
            try:
                cur = conn.execute("SELECT id, case_id, source, hearing_date, outcome FROM hearing")
                groups = {}
                for r in cur:
                    hid = r["id"]
                    case_id = r["case_id"]
                    source = r["source"]
                    hearing_date = r["hearing_date"]
                    outcome = r["outcome"]
                    # Only consider rows with hearing_date not null
                    if hearing_date is None:
                        continue
                    normalized_outcome = normalize_outcome_for_index(outcome)
                    key = (case_id, source, str(hearing_date), normalized_outcome)
                    groups.setdefault(key, []).append(hid)
                dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
                if dup_groups:
                    report_lines.append(f"Found {len(dup_groups)} duplicate groups by (case_id, source, hearing_date, normalized_outcome):")
                    for k, ids in dup_groups.items():
                        case_id, source, hearing_date_str, norm_out = k
                        report_lines.append(f"  case_id={case_id} source={source} hearing_date={hearing_date_str} normalized_outcome='{norm_out}' hearing_ids={ids}")
                else:
                    report_lines.append("No duplicates found by (case_id, source, hearing_date, normalized_outcome).")
            except Exception as e:
                report_lines.append(f"Error while computing outcome-normalized duplicates: {e}")
    else:
        report_lines.append("Hearing provenance columns (source, source_id) not fully present; skipping hearing duplicate checks that need those columns.")
        # If only some exist, report specifically
        missing = [c for c in ["source", "source_id", "synced_at"] if c not in hearing_cols]
        report_lines.append(f"Missing hearing columns required for provenance: {missing}")

    # Provide a brief summary / advice
    report_lines.append("\nSummary & next steps:")
    report_lines.append("- This script performed read-only checks. If you plan to add unique indexes,")
    report_lines.append("  you must ensure the DB has no duplicate groups as reported above.")
    report_lines.append("- If hearing.source/source_id columns are missing, run ecourts.migrations.apply_ecourts_migrations_if_needed()")
    report_lines.append("  against a COPY of the DB and follow the backfill + duplicate-detection steps before creating indexes.")
    report_lines.append("- Always work on a DB copy and keep a full backup before applying migrations or indexes.")

    conn.close()
    return "\n".join(report_lines)


if __name__ == "__main__":
    print(inspect_db())

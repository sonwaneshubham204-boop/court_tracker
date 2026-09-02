import importlib
import os
import sys
from datetime import date, datetime
import pytest

# Ensure we use an isolated in-memory SQLite DB for the app import.
# We import the app module after setting DATABASE_URL so the module-level
# SQLAlchemy instance binds to the test DB (never touch court_tracker.db).
TEST_DB_URL = "sqlite:///:memory:"


@pytest.fixture(scope="function", autouse=True)
def isolated_app_db():
    """
    Import (or reload) the application's app module configured to use an
    in-memory SQLite database so tests run against an isolated DB.
    Also apply ecourts migrations (idempotent) to create sync_log and hearing
    provenance columns required by SyncService.
    """
    # Set env var before import so app.py picks it up
    os.environ["DATABASE_URL"] = TEST_DB_URL

    # Remove cached modules to ensure a fresh import bound to the in-memory DB.
    for mod in ("app", "ecourts.migrations"):
        if mod in sys.modules:
            del sys.modules[mod]

    # Import app (this runs the module-level DB setup)
    import importlib as _importlib
    app_mod = _importlib.import_module("app")

    # Ensure DB is clean and created for tests
    from ecourts.migrations import apply_ecourts_migrations_if_needed

    # Keep the Flask application context active for the entire test.
    # SyncService and SQLAlchemy queries need an active app context, not just
    # the setup phase.
    with app_mod.app.app_context():
        # Drop/create to be deterministic per test
        app_mod.db.drop_all()
        app_mod.db.create_all()
        # Apply ecourts migrations to create sync_log + hearing columns + case extras
        apply_ecourts_migrations_if_needed()

        try:
            yield app_mod
        finally:
            # teardown: remove module so subsequent tests start fresh
            for mod in ("app", "ecourts.migrations"):
                if mod in sys.modules:
                    del sys.modules[mod]
    for mod in ("app", "ecourts.migrations"):
        if mod in sys.modules:
            del sys.modules[mod]


def create_case(app_mod, crn_no, case_no="CASE-1", court_no=1, parties="A vs B", case_stage="Hearing", next_hearing=None):
    """
    Create a valid Case record using the actual application's Case model.
    The Case model requires non-null fields: case_no, court_no, parties, case_stage.
    """
    from app import db
    Case = app_mod.Case
    c = Case(
        case_no=str(case_no),
        court_no=int(court_no),
        parties=str(parties),
        case_stage=str(case_stage),
        crn_no=(str(crn_no).strip() if crn_no is not None else None),
        normalized_crn=(str(crn_no).strip().casefold() if crn_no is not None else None),
        next_hearing_date=next_hearing
    )
    db.session.add(c)
    db.session.commit()
    return c


def create_hearing(app_mod, case_id, hearing_date=None, outcome="Hearing", source="user", source_id=None, next_hearing_date=None):
    """
    Create a Hearing using the app's Hearing model. Outcome is required.
    """
    from app import db
    Hearing = app_mod.Hearing
    outcome_value = outcome or "Hearing"
    h = Hearing(
        case_id=case_id,
        hearing_date=(hearing_date or date.today()),
        outcome=outcome_value,
        outcome_normalized=" ".join(str(outcome_value).lower().split()),
        next_hearing_date=next_hearing_date,
        notes=None
    )
    # provenance fields
    h.source = source
    if source_id:
        h.source_id = source_id
    h.synced_at = datetime.utcnow()
    db.session.add(h)
    db.session.commit()
    return h


def normalize(payload):
    from ecourts.normalizer import normalize_provider_payload
    return normalize_provider_payload(payload)


def test_normalizer_cnr_aliases_and_date_parsing(isolated_app_db):
    app_mod = isolated_app_db
    payload = {
        "crn_no": "  ABC/123  ",
        "hearingDate": "02-09-2026",
        "nextHearingDate": "2026-10-01",
        "outcome": "Order Passed",
        "id": "remote-1"
    }
    n = normalize(payload)
    assert n["cnr"] == "ABC/123"
    assert n["normalized_crn"] == "abc/123"
    assert n["hearing_date"] == date(2026, 9, 2)
    assert n["next_hearing_date"] == date(2026, 10, 1)
    assert n["source_id"] == "remote-1"
    assert n["outcome_normalized"] == "order passed"


def test_missing_cnr_logs_and_skips(isolated_app_db):
    app_mod = isolated_app_db
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({"hearing_date": "2026-09-02", "next_hearing_date": "2026-10-01"})
    res = svc.sync_case_from_data(payload)
    assert not res.success
    assert "Missing CNR" in res.message
    # Ensure sync_log exists and an entry was appended (best-effort)
    inspector = db.inspect(db.engine)
    assert "sync_log" in inspector.get_table_names()
    cnt = db.session.execute(db.text("SELECT COUNT(*) FROM sync_log")).scalar()
    assert cnt >= 1


def test_unmatched_cnr_creates_log_and_returns_false(isolated_app_db):
    app_mod = isolated_app_db
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({"cnr": "NONEXISTENT", "next_hearing_date": "2026-11-01"})
    res = svc.sync_case_from_data(payload)
    assert not res.success
    assert "No matching local case" in res.message or "No matching local case found" in res.message
    inspector = db.inspect(db.engine)
    assert "sync_log" in inspector.get_table_names()
    cnt = db.session.execute(db.text("SELECT COUNT(*) FROM sync_log")).scalar()
    assert cnt >= 1


def test_cnr_case_insensitive_trim_matching(isolated_app_db):
    app_mod = isolated_app_db
    # create a case with padded/capitalized crn
    c = create_case(app_mod, " AbC-123 ", case_no="C1")
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({"cnr": "abc-123", "next_hearing_date": "2026-12-01"})
    res = svc.sync_case_from_data(payload)
    assert res.success
    # reload and check
    fresh = app_mod.Case.query.get(c.id)
    assert fresh.next_hearing_date == date(2026, 12, 1)
    assert fresh.normalized_crn == "abc-123"


def test_ambiguous_cnr_marks_cases_and_logs(isolated_app_db):
    app_mod = isolated_app_db
    # create two cases with same CNR (different whitespace/case) -> ambiguous
    c1 = create_case(app_mod, "X-1", case_no="A1")
    c2 = create_case(app_mod, " x-1 ", case_no="A2")
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({"cnr": "X-1", "next_hearing_date": "2026-12-31"})
    res = svc.sync_case_from_data(payload)
    assert not res.success
    refreshed1 = app_mod.Case.query.get(c1.id)
    refreshed2 = app_mod.Case.query.get(c2.id)
    assert refreshed1.sync_status == svc.SYNC_STATUS_AMBIGUOUS
    assert refreshed2.sync_status == svc.SYNC_STATUS_AMBIGUOUS
    # a log entry should exist
    cnt = db.session.execute(db.text("SELECT COUNT(*) FROM sync_log")).scalar()
    assert cnt >= 1


def test_no_change_sync_sets_no_change_status_and_timestamp(isolated_app_db):
    app_mod = isolated_app_db
    nhd = date(2026, 9, 10)
    c = create_case(app_mod, "NC-1", case_no="NC1", next_hearing=nhd)
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({"cnr": "NC-1", "next_hearing_date": nhd})
    res = svc.sync_case_from_data(payload)
    assert res.success
    fresh = app_mod.Case.query.get(c.id)
    assert fresh.sync_status == svc.SYNC_STATUS_NO_CHANGE
    assert getattr(fresh, "last_synced_at", None) is not None


def test_changed_next_hearing_creates_ecourts_hearing_and_updates_case(isolated_app_db):
    app_mod = isolated_app_db
    c = create_case(app_mod, "CH-1", case_no="CH1")
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({
        "cnr": "CH-1",
        "hearing_date": "2026-09-02",
        "next_hearing_date": "2026-11-05",
        "outcome": "Order Passed",
        "order_info": "Some order text",
        "source_id": "rid-123"
    })
    res = svc.sync_case_from_data(payload)
    assert res.success
    fresh_case = app_mod.Case.query.get(c.id)
    assert fresh_case.next_hearing_date == date(2026, 11, 5)
    assert fresh_case.sync_status == svc.SYNC_STATUS_OK
    # hearing created with source="ecourts" and source_id set
    hearing = app_mod.Hearing.query.filter_by(case_id=c.id, source="ecourts", source_id="rid-123").first()
    assert hearing is not None
    assert hearing.outcome is not None
    assert hearing.outcome_normalized == "order passed"
    assert fresh_case.normalized_crn == "ch-1"


def test_duplicate_prevention_by_source_id(isolated_app_db):
    app_mod = isolated_app_db
    c = create_case(app_mod, "DUP-1", case_no="DUP1")
    # create an existing ecourts hearing with same source_id
    create_hearing(app_mod, c.id, hearing_date=date(2026, 9, 2), outcome="Order Passed", source="ecourts", source_id="dup-1")
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({
        "cnr": "DUP-1",
        "hearing_date": "2026-09-02",
        "next_hearing_date": "2026-12-01",
        "outcome": "Order Passed",
        "source_id": "dup-1"
    })
    res = svc.sync_case_from_data(payload)
    assert res.success
    rows = app_mod.Hearing.query.filter_by(case_id=c.id, source="ecourts", source_id="dup-1").all()
    assert len(rows) == 1


def test_duplicate_prevention_by_date_and_normalized_outcome(isolated_app_db):
    app_mod = isolated_app_db
    c = create_case(app_mod, "DUP2", case_no="DUP2")
    # create existing ecourts hearing with same date and outcome that differs by spacing/case
    create_hearing(app_mod, c.id, hearing_date=date(2026, 9, 2), outcome="  order Passed  ", source="ecourts")
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({
        "cnr": "DUP2",
        "hearing_date": "2026-09-02",
        "next_hearing_date": "2026-12-01",
        "outcome": "Order    passed"
    })
    res = svc.sync_case_from_data(payload)
    assert res.success
    rows = app_mod.Hearing.query.filter_by(case_id=c.id, source="ecourts", hearing_date=date(2026, 9, 2)).all()
    # Duplicate prevention should keep exactly one ecourts-sourced hearing for that date
    assert len(rows) == 1


def test_rollback_and_error_handling_marks_case_sync_error(isolated_app_db, monkeypatch):
    app_mod = isolated_app_db
    c = create_case(app_mod, "ERR-1", case_no="ERR1")
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({
        "cnr": "ERR-1",
        "hearing_date": "2026-09-02",
        "next_hearing_date": "2026-12-01",
        "outcome": "Order"
    })

    # Monkeypatch db.session.commit to raise on the first call and succeed thereafter
    original_commit = db.session.commit
    call_count = {"n": 0}

    def flaky_commit():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated commit failure")
        return original_commit()

    monkeypatch.setattr(db.session, "commit", flaky_commit)

    res = svc.sync_case_from_data(payload)
    assert not res.success
    fresh = app_mod.Case.query.get(c.id)
    assert fresh.sync_status == svc.SYNC_STATUS_ERROR
    assert getattr(fresh, "last_sync_error", None) is not None


def test_apply_migrations_idempotent(isolated_app_db):
    app_mod = isolated_app_db
    # Call migrations twice; should be idempotent and not raise
    from ecourts.migrations import apply_ecourts_migrations_if_needed
    apply_ecourts_migrations_if_needed()
    apply_ecourts_migrations_if_needed()
    db = app_mod.db
    inspector = db.inspect(db.engine)
    assert "sync_log" in inspector.get_table_names()


def test_preserve_existing_manual_hearing_when_ecourts_syncs(isolated_app_db):
    """
    Ensure an existing user-created hearing (source='user') is preserved when
    an ecourts sync creates an ecourts-sourced hearing for the same case.
    """
    app_mod = isolated_app_db
    c = create_case(app_mod, "USER-1", case_no="U1")
    # create manual/user hearing
    create_hearing(app_mod, c.id, hearing_date=date(2026, 9, 1), outcome="Hearing", source="user")
    from ecourts.service import SyncService
    db = app_mod.db
    svc = SyncService(db_session=db)
    payload = normalize({
        "cnr": "USER-1",
        "hearing_date": "2026-09-02",
        "next_hearing_date": "2026-10-01",
        "outcome": "Adjourned",
        "source_id": "user-ecourt-1"
    })
    res = svc.sync_case_from_data(payload)
    assert res.success
    # After sync, ensure user hearing remains and ecourts hearing added
    user_rows = app_mod.Hearing.query.filter_by(case_id=c.id, source="user").all()
    ec_rows = app_mod.Hearing.query.filter_by(case_id=c.id, source="ecourts").all()
    assert len(user_rows) == 1
    assert len(ec_rows) == 1


# ---------------------------------------------------------------------------
# eCourts client -> SyncService integration tests
# ---------------------------------------------------------------------------

def test_sync_case_by_cnr_fetches_normalizes_and_syncs(isolated_app_db):
    """
    Verify the new client -> normalizer -> SyncService pipeline.

    The fake client performs no network access. It returns a provider-shaped
    payload, which SyncService must normalize and then pass through the
    existing sync_case_from_data() pipeline.
    """
    app_mod = isolated_app_db
    c = create_case(app_mod, "  INT-001  ", case_no="INT1")

    from ecourts.service import SyncService

    class FakeEcourtsClient:
        def __init__(self):
            self.requested_cnr = None

        def fetch_case_by_cnr(self, cnr):
            self.requested_cnr = cnr
            return {
                "crn_no": " int-001 ",
                "case_no": "REMOTE-CASE-1",
                "court_no": 7,
                "parties": "Party A vs Party B",
                "advocate": "Advocate X",
                "case_status": "Evidence",
                "nextHearingDate": "2026-12-15",
                "hearingDate": "2026-09-02",
                "outcome": "Order Passed",
                "id": "remote-int-001",
            }

    db = app_mod.db
    client = FakeEcourtsClient()
    svc = SyncService(db_session=db)

    res = svc.sync_case_by_cnr("  INT-001  ", client=client)

    assert res.success
    assert client.requested_cnr == "INT-001"

    fresh = app_mod.Case.query.get(c.id)
    assert fresh is not None
    assert fresh.normalized_crn == "int-001"
    assert fresh.case_no == "REMOTE-CASE-1"
    assert fresh.court_no == 7
    assert fresh.parties == "Party A vs Party B"
    assert fresh.advocate_name == "Advocate X"
    assert fresh.case_stage == "Evidence"
    assert fresh.next_hearing_date == date(2026, 12, 15)
    assert fresh.sync_status == svc.SYNC_STATUS_OK

    hearing = app_mod.Hearing.query.filter_by(
        case_id=c.id,
        source="ecourts",
        source_id="remote-int-001",
    ).first()
    assert hearing is not None
    assert hearing.hearing_date == date(2026, 9, 2)
    assert hearing.outcome_normalized == "order passed"


def test_sync_case_by_cnr_missing_cnr_does_not_call_client(isolated_app_db):
    app_mod = isolated_app_db

    from ecourts.service import SyncService

    class FailingClient:
        def fetch_case_by_cnr(self, cnr):
            raise AssertionError("Client must not be called for missing CNR")

    svc = SyncService(db_session=app_mod.db)

    res = svc.sync_case_by_cnr("   ", client=FailingClient())

    assert not res.success
    assert "Missing CNR" in res.message


def test_sync_case_by_cnr_client_not_found_returns_failure(isolated_app_db):
    app_mod = isolated_app_db
    create_case(app_mod, "NF-001", case_no="NF1")

    from ecourts.service import SyncService

    class NotFoundClient:
        def fetch_case_by_cnr(self, cnr):
            return None

    svc = SyncService(db_session=app_mod.db)

    res = svc.sync_case_by_cnr("NF-001", client=NotFoundClient())

    assert not res.success
    assert "not found" in res.message.lower()


def test_sync_case_by_cnr_invalid_provider_payload_returns_failure(isolated_app_db):
    app_mod = isolated_app_db

    from ecourts.service import SyncService

    class InvalidPayloadClient:
        def fetch_case_by_cnr(self, cnr):
            return ["not", "a", "dict"]

    svc = SyncService(db_session=app_mod.db)

    res = svc.sync_case_by_cnr("INVALID-001", client=InvalidPayloadClient())

    assert not res.success
    assert "invalid payload" in res.message.lower()


def test_sync_case_by_cnr_client_error_is_handled(isolated_app_db):
    app_mod = isolated_app_db

    from ecourts.service import SyncService

    class ErrorClient:
        def fetch_case_by_cnr(self, cnr):
            raise RuntimeError("simulated provider failure")

    svc = SyncService(db_session=app_mod.db)

    res = svc.sync_case_by_cnr("ERR-CLIENT-001", client=ErrorClient())

    assert not res.success
    assert "client error" in res.message.lower()
    assert "simulated provider failure" in res.message


def test_sync_case_by_cnr_uses_null_client_without_network(isolated_app_db):
    app_mod = isolated_app_db

    from ecourts.service import SyncService

    svc = SyncService(db_session=app_mod.db)

    res = svc.sync_case_by_cnr("NULL-CLIENT-001")

    assert not res.success
    assert "not implemented" in res.message.lower()

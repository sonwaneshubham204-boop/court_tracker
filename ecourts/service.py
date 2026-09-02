"""
ecourts.service

Synchronization service for eCourts-normalized payloads.

Key behaviors implemented here:
- CNR-first matching (CNR must be present for automatic updates)
- Safe change detection on next_hearing_date
- Creation of ecourts-sourced Hearing rows (source='ecourts')
- Duplicate prevention:
    * by stable source_id when provided
    * by (hearing_date + normalized outcome) among previous ecourts-sourced hearings
- Transactional updates with robust rollback handling:
    * on exception, rollback transaction, re-query case, record sync_status='sync_error' and last_sync_error
    * append an audit row to sync_log for every attempt/result
- No network calls or scraping
"""
from datetime import datetime
from typing import Dict, Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


class SyncResult:
    def __init__(self, success: bool, message: str = "", data: Optional[Dict[str, Any]] = None):
        self.success = success
        self.message = message
        self.data = data or {}


class SyncService:
    SYNC_STATUS_NEVER = "never_synced"
    SYNC_STATUS_OK = "synced_ok"
    SYNC_STATUS_NO_CHANGE = "no_change"
    SYNC_STATUS_ERROR = "sync_error"
    SYNC_STATUS_AMBIGUOUS = "ambiguous_match"

    def __init__(self, db_session=None):
        # allow injection of a db/session for testing; otherwise import lazily
        self._db_override = db_session

    def _db(self):
        if self._db_override is not None:
            return self._db_override
        # lazy import
        from app import db as app_db
        return app_db

    def _log(self, case_id, success, old_date, new_date, source, payload_text, error_message=None):
        """Append-only sync log using a raw SQL INSERT. Caller manages commit/rollback."""
        db = self._db()
        try:
            insert_sql = text(
                "INSERT INTO sync_log (case_id, success, old_next_hearing_date, new_next_hearing_date, source, raw_payload, error_message, created_at) "
                "VALUES (:case_id, :success, :old, :new, :source, :payload, :error, CURRENT_TIMESTAMP)"
            )
            db.session.execute(insert_sql, {
                "case_id": case_id,
                "success": bool(success),
                "old": old_date,
                "new": new_date,
                "source": source,
                "payload": payload_text,
                "error": error_message
            })
            # do not commit here; leave commit/rollback to the calling flow
        except Exception:
            # best-effort; do not raise from logging to avoid masking original errors
            pass

    @staticmethod
    def _normalize_text(s: Optional[str]) -> str:
        if s is None:
            return ""
        return " ".join(str(s).lower().split())

    @staticmethod
    def _normalize_crn(s: Optional[str]) -> str:
        if s is None:
            return ""
        return str(s).strip().casefold()

    @staticmethod
    def _apply_case_fields(case, payload):
        """Apply available normalized eCourts fields to the local Case."""
        field_map = {
            "case_no": "case_no",
            "court_no": "court_no",
            "parties": "parties",
            "advocate": "advocate_name",
            "case_status": "case_stage",
        }

        for source_field, target_field in field_map.items():
            value = payload.get(source_field)
            if value is not None and hasattr(case, target_field):
                setattr(case, target_field, value)

        if payload.get("cnr"):
            case.crn_no = str(payload["cnr"]).strip()
            case.normalized_crn = (
                payload.get("normalized_crn")
                or SyncService._normalize_crn(payload["cnr"])
            )

    def sync_case_from_data(self, payload: Dict[str, Any]):
        """
        Accepts a normalized payload (see ecourts.normalizer.normalize_provider_payload).
        Expects keys such as: cnr, case_no, court_no, parties, advocate, case_status,
        hearing_date, next_hearing_date, outcome, order_info, source_id.

        Returns SyncResult(success, message, data).
        """
        # local imports to avoid circular import at module import time
        db = self._db()
        from app import Case, Hearing

        # robust CNR normalization
        raw_cnr = payload.get("cnr")
        cnr = None
        if raw_cnr is not None:
            try:
                cnr = str(raw_cnr).strip()
            except Exception:
                cnr = None

        payload_text = str(payload)

        if not cnr:
            # Missing CNR -> do not auto-update; log and return
            try:
                self._log(None, False, None, payload.get("next_hearing_date"), "ecourts", payload_text,
                          error_message="Missing CNR; auto-update skipped.")
                db.session.commit()
            except Exception:
                db.session.rollback()
            return SyncResult(False, "Missing CNR; no automatic update performed.")

        # Case-insensitive match on crn_no (try SQL trim/lower, fallback to equality)
        matches = []
        normalized_cnr = payload.get("normalized_crn") or self._normalize_crn(cnr)
        try:
            matches = Case.query.filter(
                Case.normalized_crn.isnot(None),
                Case.normalized_crn == normalized_cnr
            ).all()
        except Exception:
            matches = []

        # Backward-compatible fallback for legacy rows, then persist canonical CNR.
        if not matches:
            try:
                matches = Case.query.filter(
                    Case.crn_no.isnot(None),
                    db.func.lower(db.func.trim(Case.crn_no)) == normalized_cnr
                ).all()
            except Exception:
                matches = Case.query.filter(Case.crn_no == cnr).all()

            if matches:
                for matched_case in matches:
                    if not getattr(matched_case, "normalized_crn", None):
                        matched_case.normalized_crn = normalized_cnr
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

        if not matches:
            try:
                self._log(None, False, None, payload.get("next_hearing_date"), "ecourts", payload_text,
                          error_message="No matching local case for CNR")
                db.session.commit()
            except Exception:
                db.session.rollback()
            return SyncResult(False, "No matching local case found for CNR.")

        if len(matches) > 1:
            # ambiguous: mark involved cases and log
            try:
                for c in matches:
                    c.sync_status = self.SYNC_STATUS_AMBIGUOUS
                # log against first match for traceability
                self._log(matches[0].id, False, None, payload.get("next_hearing_date"), "ecourts", payload_text,
                          error_message=f"Ambiguous matches for CNR: {[m.id for m in matches]}")
                db.session.commit()
            except Exception:
                db.session.rollback()
            return SyncResult(False, "Ambiguous matches for CNR; no automatic update.")

        case = matches[0]
        remote_next = payload.get("next_hearing_date")
        local_next = case.next_hearing_date

        # If both None or equal -> no change
        if (remote_next is None and local_next is None) or (remote_next == local_next):
            try:
                case.sync_status = self.SYNC_STATUS_NO_CHANGE
                case.last_synced_at = datetime.utcnow()
                self._log(case.id, True, local_next, remote_next, "ecourts", payload_text)
                db.session.commit()
            except Exception:
                db.session.rollback()
            return SyncResult(True, "No change detected.")

        # remote differs -> attempt to update safely
        try:
            # Apply available case-level eCourts fields before hearing sync.
            self._apply_case_fields(case, payload)

            source_id = payload.get("source_id")
            # duplicate prevention by source_id
            if source_id:
                existing = Hearing.query.filter_by(case_id=case.id, source_id=source_id).first()
                if existing:
                    case.sync_status = self.SYNC_STATUS_NO_CHANGE
                    case.last_synced_at = datetime.utcnow()
                    self._log(case.id, True, local_next, remote_next, "ecourts", payload_text,
                              error_message="Duplicate detected by source_id; no insertion.")
                    db.session.commit()
                    return SyncResult(True, "Duplicate by source_id; no action taken.")

            # prepare payload hearing date for comparison (coerce datetime -> date)
            payload_hearing_date = payload.get("hearing_date")
            if hasattr(payload_hearing_date, "date"):
                payload_hearing_date = payload_hearing_date.date()

            # duplicate prevention by (hearing_date + normalized outcome) among eCourts hearings
            payload_outcome_normalized = payload.get("outcome_normalized") or self._normalize_text(payload.get("outcome"))
            if not source_id:
                candidates = Hearing.query.filter_by(case_id=case.id, source="ecourts").all()
                for h in candidates:
                    existing_outcome_normalized = getattr(h, "outcome_normalized", None) or self._normalize_text(h.outcome)
                    if h.hearing_date == payload_hearing_date and existing_outcome_normalized == payload_outcome_normalized:
                        case.sync_status = self.SYNC_STATUS_NO_CHANGE
                        case.last_synced_at = datetime.utcnow()
                        self._log(case.id, True, local_next, remote_next, "ecourts", payload_text,
                                  error_message="Duplicate detected by date+outcome; no insertion.")
                        db.session.commit()
                        return SyncResult(True, "Duplicate by date/outcome; no action taken.")

            # Create ecourts-sourced hearing
            new_hearing = Hearing(
                case_id=case.id,
                hearing_date=payload.get("hearing_date") or datetime.utcnow().date(),
                outcome=payload.get("outcome") or "Hearing",
                presentee=None,
                business=None,
                next_hearing_date=payload.get("next_hearing_date"),
                notes=payload.get("order_info") or None
            )
            # provenance
            new_hearing.source = "ecourts"
            if source_id:
                new_hearing.source_id = source_id
            new_hearing.synced_at = datetime.utcnow()
            new_hearing.outcome_normalized = payload_outcome_normalized

            db.session.add(new_hearing)

            old_next = case.next_hearing_date
            case.next_hearing_date = payload.get("next_hearing_date")
            case.last_synced_at = datetime.utcnow()
            case.sync_status = self.SYNC_STATUS_OK
            if payload.get("source_id"):
                case.ecourts_id = payload.get("source_id")

            self._log(case.id, True, old_next, case.next_hearing_date, "ecourts", payload_text)
            try:
                db.session.commit()
            except IntegrityError as ie:
                # DB uniqueness protects against concurrent duplicate insertion.
                db.session.rollback()
                fresh_case = Case.query.get(case.id)
                if fresh_case is not None:
                    fresh_case.sync_status = self.SYNC_STATUS_NO_CHANGE
                    fresh_case.last_synced_at = datetime.utcnow()
                self._log(
                    case.id, True, old_next, case.next_hearing_date, "ecourts",
                    payload_text,
                    error_message=f"IntegrityError on duplicate insert; treated as no-op: {ie}"
                )
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return SyncResult(True, "Duplicate (concurrent) detected; no action taken.")
            return SyncResult(True, "Synced: next hearing date updated.", data={"case_id": case.id})

        except Exception as e:
            # Rollback and record failure. Re-query the case to ensure we can update a fresh instance.
            db.session.rollback()
            try:
                case_id = None
                if "case" in locals() and case is not None:
                    case_id = getattr(case, "id", None)
                if case_id is not None:
                    fresh_case = Case.query.get(case_id)
                    if fresh_case is not None:
                        fresh_case.sync_status = self.SYNC_STATUS_ERROR
                        fresh_case.last_sync_error = str(e)
                        fresh_case.last_synced_at = datetime.utcnow()
                # append a log entry describing the error
                self._log(case_id, False, local_next, payload.get("next_hearing_date"), "ecourts", payload_text, error_message=str(e))
                db.session.commit()
            except Exception:
                db.session.rollback()
            return SyncResult(False, f"Error during sync: {e}")

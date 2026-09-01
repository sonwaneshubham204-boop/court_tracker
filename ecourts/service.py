"""
Synchronization service for eCourts-normalized payloads.

This service implements a safe, CNR-first synchronization flow that:
- Requires CNR for automatic updates
- Detects changes to next_hearing_date
- Creates ecourts-sourced Hearing rows (source='ecourts')
- Updates Case.next_hearing_date only after validation
- Records append-only sync_log entries via raw SQL to avoid ORM model circular imports
- Uses transactions and preserves existing manual hearing history

No network calls are made by this module.
"""
from datetime import datetime
from typing import Dict, Any, Optional

from sqlalchemy import text


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
        # lazy import to avoid circular imports when app imports ecourts
        self.db = db_session

    def _get_db(self):
        if self.db is not None:
            return self.db
        # import on demand
        from app import db as app_db
        return app_db

    def _log(self, case_id, success, old_date, new_date, source, payload_text, error_message=None):
        db = self._get_db()
        # Use raw SQL insert into sync_log (append-only). Table is created by migrations.
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
            # Do not commit here; caller will commit/rollback transaction as appropriate.
        except Exception:
            # best-effort logging; swallow to avoid raising during rollback paths
            pass

    @staticmethod
    def _normalize_text(s: Optional[str]) -> str:
        if s is None:
            return ""
        return " ".join(str(s).lower().split())

    def sync_case_from_data(self, payload: Dict[str, Any]):
        """Synchronize a single normalized provider payload.

        payload must be normalized (see ecourts.normalizer). Key behavior:
          - CNR required for automatic updates
          - If CNR missing -> no auto-update; append log
          - If multiple matches -> ambiguous -> no auto-update; append log
          - If single match and next_hearing_date differs -> create ecourts-sourced Hearing and update Case.next_hearing_date within a transaction
        """
        from app import db, Case, Hearing  # local import to avoid circular import at module load

        cnr = payload.get("cnr")
        payload_text = str(payload)

        if not cnr:
            # missing CNR: record log and do not update
            try:
                self._log(None, False, None, payload.get("next_hearing_date"), "ecourts", payload_text,
                          error_message="Missing CNR; auto-update skipped.")
                db.session.commit()
            except Exception:
                db.session.rollback()
            return SyncResult(False, "Missing CNR; no automatic update performed.")

        # case-insensitive match on crn_no
        matches = Case.query.filter(Case.crn_no.isnot(None), db.func.lower(Case.crn_no) == cnr.lower()).all()

        if not matches:
            try:
                self._log(None, False, None, payload.get("next_hearing_date"), "ecourts", payload_text,
                          error_message="No matching local case for CNR")
                db.session.commit()
            except Exception:
                db.session.rollback()
            return SyncResult(False, "No matching local case found for CNR.")

        if len(matches) > 1:
            # ambiguous
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

        # if both None or equal -> no change
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

            # duplicate prevention by (hearing_date + outcome) among ecourts-sourced hearings
            if not source_id:
                candidates = Hearing.query.filter_by(case_id=case.id, source="ecourts").all()
                for h in candidates:
                    if h.hearing_date == payload.get("hearing_date") and (self._normalize_text(h.outcome) == self._normalize_text(payload.get("outcome"))):
                        case.sync_status = self.SYNC_STATUS_NO_CHANGE
                        case.last_synced_at = datetime.utcnow()
                        self._log(case.id, True, local_next, remote_next, "ecourts", payload_text,
                                  error_message="Duplicate detected by date+outcome; no insertion.")
                        db.session.commit()
                        return SyncResult(True, "Duplicate by date/outcome; no action taken.")

            # create ecourts-sourced hearing
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

            db.session.add(new_hearing)
            old_next = case.next_hearing_date
            case.next_hearing_date = payload.get("next_hearing_date")
            case.last_synced_at = datetime.utcnow()
            case.sync_status = self.SYNC_STATUS_OK
            if payload.get("source_id"):
                case.ecourts_id = payload.get("source_id")

            self._log(case.id, True, old_next, case.next_hearing_date, "ecourts", payload_text)
            db.session.commit()
            return SyncResult(True, "Synced: next hearing date updated.", data={"case_id": case.id})
        except Exception as e:
            db.session.rollback()
            try:
                case.sync_status = self.SYNC_STATUS_ERROR
                case.last_sync_error = str(e)
                self._log(case.id, False, local_next, remote_next, "ecourts", payload_text, error_message=str(e))
                db.session.commit()
            except Exception:
                db.session.rollback()
            return SyncResult(False, f"Error during sync: {e}")

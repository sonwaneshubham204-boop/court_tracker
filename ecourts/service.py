"""
ecourts.service

Synchronization service for the Court Tracker eCourts integration.

Locked scope:
- CNR-first matching
- eCourts automatic update of ONLY the existing Case.next_hearing_date
- No eCourts Hearing History insertion
- No Judge / Act-Section / Transfer synchronization
- Existing manual Case History / Hearing functionality remains untouched
- No network access in this service; provider access is handled by the client
"""
from datetime import datetime
from typing import Dict, Any, Optional

from sqlalchemy import text

from ecourts.normalizer import normalize_provider_payload


class SyncResult:
    def __init__(
        self,
        success: bool,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ):
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
        # Allow injection of a db/session for testing; otherwise import lazily.
        self._db_override = db_session

    def _db(self):
        if self._db_override is not None:
            return self._db_override

        from app import db as app_db

        return app_db

    def _log(
        self,
        case_id,
        success,
        old_date,
        new_date,
        source,
        payload_text,
        error_message=None,
    ):
        """Append an audit row. Caller manages commit/rollback."""
        db = self._db()

        try:
            insert_sql = text(
                "INSERT INTO sync_log "
                "(case_id, success, old_next_hearing_date, "
                "new_next_hearing_date, source, raw_payload, "
                "error_message, created_at) "
                "VALUES (:case_id, :success, :old, :new, :source, "
                ":payload, :error, CURRENT_TIMESTAMP)"
            )

            db.session.execute(
                insert_sql,
                {
                    "case_id": case_id,
                    "success": bool(success),
                    "old": old_date,
                    "new": new_date,
                    "source": source,
                    "payload": payload_text,
                    "error": error_message,
                },
            )
        except Exception:
            # Logging must never mask the original sync operation.
            pass

    @staticmethod
    def _normalize_crn(value: Optional[str]) -> str:
        if value is None:
            return ""

        return str(value).strip().casefold()

    def sync_case_by_cnr(self, cnr: str, client=None):
        """
        Fetch a case by CNR and synchronize ONLY its next hearing date.

        The provider client performs the external lookup.
        This method never writes provider data directly except through
        sync_case_from_data().
        """
        if cnr is None or not str(cnr).strip():
            return SyncResult(
                False,
                "Missing CNR; no automatic update performed.",
            )

        if client is None:
            from ecourts.client import NullEcourtsClient

            client = NullEcourtsClient()

        try:
            provider_payload = client.fetch_case_by_cnr(str(cnr).strip())
        except NotImplementedError as exc:
            return SyncResult(False, str(exc))
        except Exception as exc:
            return SyncResult(False, f"eCourts client error: {exc}")

        if provider_payload is None:
            return SyncResult(False, "eCourts case not found.")

        if not isinstance(provider_payload, dict):
            return SyncResult(
                False,
                "eCourts client returned an invalid payload.",
            )

        normalized_payload = normalize_provider_payload(provider_payload)

        if not normalized_payload.get("cnr"):
            normalized_payload["cnr"] = str(cnr).strip()
            normalized_payload["normalized_crn"] = self._normalize_crn(cnr)

        return self.sync_case_from_data(normalized_payload)

    def sync_case_from_data(self, payload: Dict[str, Any]):
        """
        Synchronize ONLY Case.next_hearing_date.

        Required matching key:
            cnr

        Accepted provider field:
            next_hearing_date

        No other case fields are automatically changed.
        In particular, this method does NOT:
        - create Hearing rows
        - update Hearing History
        - update Judge
        - update Act/Section
        - update Transfer details
        - update Case Number, Court Number, Parties, Advocate, or Case Stage
        """
        db = self._db()
        from app import Case

        raw_cnr = payload.get("cnr")
        cnr = None

        if raw_cnr is not None:
            try:
                cnr = str(raw_cnr).strip()
            except Exception:
                cnr = None

        payload_text = str(payload)

        if not cnr:
            try:
                self._log(
                    None,
                    False,
                    None,
                    payload.get("next_hearing_date"),
                    "ecourts",
                    payload_text,
                    error_message="Missing CNR; auto-update skipped.",
                )
                db.session.commit()
            except Exception:
                db.session.rollback()

            return SyncResult(
                False,
                "Missing CNR; no automatic update performed.",
            )

        normalized_cnr = (
            payload.get("normalized_crn")
            or self._normalize_crn(cnr)
        )

        matches = []

        # Preferred match: canonical normalized_crn.
        try:
            matches = Case.query.filter(
                Case.normalized_crn.isnot(None),
                Case.normalized_crn == normalized_cnr,
            ).all()
        except Exception:
            matches = []

        # Backward-compatible fallback for older Case rows.
        if not matches:
            try:
                matches = Case.query.filter(
                    Case.crn_no.isnot(None),
                    db.func.lower(db.func.trim(Case.crn_no))
                    == normalized_cnr,
                ).all()
            except Exception:
                matches = Case.query.filter(
                    Case.crn_no == cnr
                ).all()

            if matches:
                for matched_case in matches:
                    if not getattr(
                        matched_case,
                        "normalized_crn",
                        None,
                    ):
                        matched_case.normalized_crn = normalized_cnr

                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

        if not matches:
            try:
                self._log(
                    None,
                    False,
                    None,
                    payload.get("next_hearing_date"),
                    "ecourts",
                    payload_text,
                    error_message="No matching local case for CNR",
                )
                db.session.commit()
            except Exception:
                db.session.rollback()

            return SyncResult(
                False,
                "No matching local case found for CNR.",
            )

        if len(matches) > 1:
            try:
                for matched_case in matches:
                    matched_case.sync_status = (
                        self.SYNC_STATUS_AMBIGUOUS
                    )

                self._log(
                    matches[0].id,
                    False,
                    matches[0].next_hearing_date,
                    payload.get("next_hearing_date"),
                    "ecourts",
                    payload_text,
                    error_message=(
                        "Ambiguous matches for CNR: "
                        f"{[item.id for item in matches]}"
                    ),
                )
                db.session.commit()
            except Exception:
                db.session.rollback()

            return SyncResult(
                False,
                "Ambiguous matches for CNR; no automatic update.",
            )

        case = matches[0]
        old_next = case.next_hearing_date
        remote_next = payload.get("next_hearing_date")

        # Nothing to update when the provider has no next date.
        if remote_next is None:
            try:
                case.sync_status = self.SYNC_STATUS_NO_CHANGE
                case.last_synced_at = datetime.utcnow()

                self._log(
                    case.id,
                    True,
                    old_next,
                    None,
                    "ecourts",
                    payload_text,
                    error_message=(
                        "eCourts returned no next hearing date; "
                        "existing date preserved."
                    ),
                )
                db.session.commit()
            except Exception:
                db.session.rollback()

            return SyncResult(
                True,
                "No eCourts next hearing date; existing date preserved.",
                data={"case_id": case.id},
            )

        # Same date: preserve existing value and only update sync metadata.
        if remote_next == old_next:
            try:
                case.sync_status = self.SYNC_STATUS_NO_CHANGE
                case.last_synced_at = datetime.utcnow()

                self._log(
                    case.id,
                    True,
                    old_next,
                    remote_next,
                    "ecourts",
                    payload_text,
                )
                db.session.commit()
            except Exception:
                db.session.rollback()

            return SyncResult(
                True,
                "No change detected.",
                data={"case_id": case.id},
            )

        # The ONLY automatic Case data update happens here.
        try:
            case.next_hearing_date = remote_next
            case.last_synced_at = datetime.utcnow()
            case.sync_status = self.SYNC_STATUS_OK
            case.last_sync_error = None

            self._log(
                case.id,
                True,
                old_next,
                case.next_hearing_date,
                "ecourts",
                payload_text,
            )

            db.session.commit()

            return SyncResult(
                True,
                "Synced: next hearing date updated.",
                data={
                    "case_id": case.id,
                    "old_next_hearing_date": old_next,
                    "new_next_hearing_date": case.next_hearing_date,
                },
            )

        except Exception as exc:
            db.session.rollback()

            try:
                fresh_case = Case.query.get(case.id)

                if fresh_case is not None:
                    fresh_case.sync_status = self.SYNC_STATUS_ERROR
                    fresh_case.last_sync_error = str(exc)
                    fresh_case.last_synced_at = datetime.utcnow()

                self._log(
                    case.id,
                    False,
                    old_next,
                    remote_next,
                    "ecourts",
                    payload_text,
                    error_message=str(exc),
                )

                db.session.commit()
            except Exception:
                db.session.rollback()

            return SyncResult(
                False,
                f"Error during sync: {exc}",
            )

"""
Normalizer for ecourts provider payloads.
Convert incoming provider payloads (dicts) into a canonical normalized structure
used by the SyncService.

This module is intentionally provider-agnostic and performs no network calls.
"""
from datetime import datetime, date
from typing import Optional, Dict, Any


def _parse_date(value: Optional[Any]) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(v, fmt).date()
            except ValueError:
                pass
    return None


def _normalize_text(s: Optional[Any]) -> Optional[str]:
    if s is None:
        return None
    return " ".join(str(s).lower().split())


def _normalize_crn(s: Optional[Any]) -> Optional[str]:
    if s is None:
        return None
    return str(s).strip().casefold()


def normalize_provider_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a provider payload into the canonical sync shape.

    Expected output keys (all optional unless noted):
      - cnr: str (primary identifier)
      - normalized_crn: str (canonical form used for matching)
      - case_no: str
      - court_no: int
      - parties: str
      - advocate: str
      - case_status: str
      - hearing_date: date
      - next_hearing_date: date
      - outcome: str
      - outcome_normalized: str
      - order_info: str
      - source_id: str (provider-specific per-hearing id)

    The normalizer is permissive and will coerce types where reasonable.
    """
    normalized = {}

    # Common aliases for fields providers might use
    cnr = payload.get("cnr") or payload.get("crn") or payload.get("crn_no") or payload.get("cnr_no")
    if cnr:
        normalized["cnr"] = str(cnr).strip()
        normalized["normalized_crn"] = _normalize_crn(cnr)

    case_no = payload.get("case_no") or payload.get("caseNumber") or payload.get("caseNumberText")
    if case_no:
        normalized["case_no"] = str(case_no).strip()

    # court number may be provided as int or string
    court_no = payload.get("court_no") or payload.get("courtNumber") or payload.get("court")
    try:
        if court_no is not None and str(court_no).strip() != "":
            normalized["court_no"] = int(str(court_no).strip())
    except (ValueError, TypeError):
        # leave absent if cannot coerce
        pass

    parties = payload.get("parties") or payload.get("party") or payload.get("parties_text")
    if parties:
        normalized["parties"] = str(parties).strip()

    advocate = payload.get("advocate") or payload.get("advocate_name") or payload.get("advocateName")
    if advocate:
        normalized["advocate"] = str(advocate).strip()

    case_status = payload.get("case_status") or payload.get("stage") or payload.get("caseStage")
    if case_status:
        normalized["case_status"] = str(case_status).strip()

    hearing_date = payload.get("hearing_date") or payload.get("hearingDate")
    nhd = payload.get("next_hearing_date") or payload.get("nextHearingDate") or payload.get("next_date")
    normalized["hearing_date"] = _parse_date(hearing_date)
    normalized["next_hearing_date"] = _parse_date(nhd)

    outcome = payload.get("outcome") or payload.get("result") or payload.get("remarks")
    if outcome:
        normalized["outcome"] = str(outcome).strip()
        normalized["outcome_normalized"] = _normalize_text(outcome)

    order_info = payload.get("order_info") or payload.get("order") or payload.get("order_text")
    if order_info:
        normalized["order_info"] = str(order_info).strip()

    source_id = payload.get("source_id") or payload.get("id") or payload.get("remote_id")
    if source_id:
        normalized["source_id"] = str(source_id).strip()

    return normalized

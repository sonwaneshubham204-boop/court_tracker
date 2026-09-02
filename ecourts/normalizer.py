"""
Normalizer for eCourts provider payloads.
Convert incoming provider payloads (dicts) into a canonical normalized structure
used by the SyncService.

This module is intentionally provider-agnostic and performs no network calls.
"""

from datetime import datetime, date
from typing import Optional, Dict, Any

from ecourts.schema import EcourtsProviderResponse


def _parse_date(value: Optional[Any]) -> Optional[date]:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        formats = (
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%Y/%m/%d",
            "%d.%m.%Y",
        )

        for fmt in formats:
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue

    return None


def _normalize_text(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None

    value = " ".join(str(value).strip().split())
    return value or None


def _normalize_crn(value: Optional[Any]) -> Optional[str]:
    value = _normalize_text(value)
    return value.casefold() if value else None


def normalize_provider_payload(
    payload: EcourtsProviderResponse | Dict[str, Any],
) -> Dict[str, Any]:
    """
    Convert a provider response into the canonical normalized dictionary.

    The accepted input follows EcourtsProviderResponse. Dict compatibility is
    retained so existing provider payloads and tests continue to work.
    """
    payload = payload or {}

    cnr = (
        payload.get("cnr")
        or payload.get("crn_no")
        or payload.get("CNR")
        or payload.get("Cnr")
    )

    normalized = {
        "cnr": _normalize_text(cnr),
        "normalized_crn": _normalize_crn(cnr),
        "case_no": _normalize_text(payload.get("case_no")),
        "court_no": payload.get("court_no"),
        "parties": _normalize_text(payload.get("parties")),
        "advocate": _normalize_text(payload.get("advocate")),
        "case_status": _normalize_text(payload.get("case_status")),
        "hearing_date": _parse_date(
            payload.get("hearing_date") or payload.get("hearingDate")
        ),
        "next_hearing_date": _parse_date(
            payload.get("next_hearing_date")
            or payload.get("nextHearingDate")
        ),
        "outcome": _normalize_text(payload.get("outcome")),
        "outcome_normalized": _normalize_text(payload.get("outcome")).casefold()
        if _normalize_text(payload.get("outcome"))
        else None,
        "order_info": _normalize_text(payload.get("order_info")),
        "source_id": _normalize_text(
            payload.get("source_id") or payload.get("id")
        ),
    }

    return normalized

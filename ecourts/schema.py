"""
Canonical eCourts provider response contract.

This module defines the provider-agnostic data shape expected by the
Court Tracker eCourts integration. It performs no network access and
does not modify the database.

Only fields already supported by the current normalizer/sync pipeline
are required for synchronization. Additional provider sections such as
orders and disposal status are represented for future integration and
are not persisted by the current SyncService.
"""

from typing import Any, Dict, List, Optional, TypedDict


class EcourtsHearingData(TypedDict, total=False):
    """Canonical representation of one eCourts hearing."""

    hearing_date: Any
    next_hearing_date: Any
    outcome: Optional[str]
    order_info: Optional[str]
    source_id: Optional[str]


class EcourtsProviderResponse(TypedDict, total=False):
    """
    Canonical provider response consumed by the eCourts foundation.

    Required for a normal CNR sync:
        cnr

    Case fields currently supported by SyncService:
        case_no, court_no, parties, advocate, case_status,
        next_hearing_date

    Hearing fields:
        hearing_date, outcome, order_info, source_id

    Future provider sections:
        hearings, orders, disposal_status
    """

    cnr: str
    case_no: Optional[str]
    court_no: Optional[Any]
    parties: Optional[str]
    advocate: Optional[str]
    case_status: Optional[str]
    hearing_date: Any
    next_hearing_date: Any
    outcome: Optional[str]
    order_info: Optional[str]
    source_id: Optional[str]

    hearings: List[EcourtsHearingData]
    orders: List[Dict[str, Any]]
    disposal_status: Optional[str]


def empty_provider_response(cnr: str) -> EcourtsProviderResponse:
    """Create a minimal canonical response for a known CNR."""
    return {
        "cnr": str(cnr).strip(),
        "hearings": [],
        "orders": [],
    }

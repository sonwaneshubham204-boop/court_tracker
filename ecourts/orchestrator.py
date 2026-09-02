"""
eCourts sync orchestration layer.

This module coordinates CNR-based synchronization without performing any
provider/network access itself. Provider access remains inside the client,
while database changes remain inside SyncService.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

from ecourts.service import SyncService


@dataclass
class SyncRunSummary:
    """Summary of one orchestration run."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    results: List[Any] = field(default_factory=list)


class EcourtsSyncOrchestrator:
    """
    Coordinate one or more CNR-based eCourts sync operations.

    Responsibilities:
    - accept CNRs/cases to synchronize
    - call SyncService.sync_case_by_cnr()
    - continue processing when one case fails
    - return a deterministic summary

    It does not:
    - call the network directly
    - modify Case/Hearing records directly
    - change the existing SyncService matching/update rules
    """

    def __init__(self, service: SyncService, client=None):
        self.service = service
        self.client = client

    @staticmethod
    def _extract_cnr(item: Any) -> Optional[str]:
        """Extract a CNR from a string or Case-like object."""
        if item is None:
            return None

        if isinstance(item, str):
            value = item.strip()
        else:
            value = getattr(item, "crn_no", None)
            value = str(value).strip() if value is not None else ""

        return value or None

    def sync_one(self, item: Any):
        """Synchronize one CNR or Case-like object."""
        cnr = self._extract_cnr(item)

        if not cnr:
            return self.service.sync_case_by_cnr("", client=self.client)

        return self.service.sync_case_by_cnr(cnr, client=self.client)

    def sync_many(self, items: Iterable[Any]) -> SyncRunSummary:
        """
        Synchronize multiple CNRs/Case-like objects.

        A failure for one item does not stop the remaining items.
        """
        summary = SyncRunSummary()

        for item in items:
            summary.total += 1
            result = self.sync_one(item)
            summary.results.append(result)

            if result.success:
                summary.succeeded += 1
            else:
                summary.failed += 1

        return summary

    def sync_all_cases(self, case_model) -> SyncRunSummary:
        """
        Synchronize every Case currently stored in Court Tracker.

        The Case model is supplied by the caller so this orchestration layer
        does not import or alter app.py.
        """
        cases = case_model.query.all()
        return self.sync_many(cases)

"""
eCourts client abstractions and placeholders.

This module defines the client contract used by the Court Tracker
eCourts sync foundation. It intentionally performs no network access.

Actual provider/network integration can be implemented later without
changing SyncService or the local database layer.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class EcourtsClientError(Exception):
    """Base exception for eCourts client/adapter errors."""


class EcourtsInvalidCNR(EcourtsClientError):
    """Raised when the supplied CNR is empty or invalid."""


class EcourtsNotFound(EcourtsClientError):
    """Raised when the provider has no case for the supplied CNR."""


class EcourtsProviderError(EcourtsClientError):
    """Raised when the eCourts provider returns an unusable/error response."""


class EcourtsClientBase(ABC):
    """Abstract contract for an eCourts client adapter.

    Implementations must provide ``fetch_case_by_cnr`` and return either
    a normalized case dictionary (compatible with ecourts.normalizer)
    or ``None`` when the case is not found.

    The client must never modify the local Court Tracker database.
    """

    @abstractmethod
    def fetch_case_by_cnr(self, cnr: str) -> Optional[Dict[str, Any]]:
        """Fetch one case using its CNR/CRN.

        Args:
            cnr: Court Tracker CNR/CRN value.

        Returns:
            A normalized dictionary compatible with
            ``normalize_provider_payload()``, or ``None`` if not found.

        Raises:
            EcourtsInvalidCNR: if the CNR is missing/invalid.
            EcourtsNotFound: if the provider explicitly reports not found.
            EcourtsProviderError: for provider/network/response errors.

        Implementations must not modify the local database.
        """
        raise NotImplementedError


class NullEcourtsClient(EcourtsClientBase):
    """Safe placeholder used until a real provider adapter is connected.

    This client performs no network access.
    """

    def fetch_case_by_cnr(self, cnr: str) -> Optional[Dict[str, Any]]:
        """Reject an unimplemented live lookup explicitly."""
        if cnr is None or not str(cnr).strip():
            raise EcourtsInvalidCNR("CNR is required")

        raise NotImplementedError("Ecourts client not implemented")

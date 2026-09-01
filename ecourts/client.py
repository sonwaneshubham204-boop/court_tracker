"""
Ecourts client abstractions and placeholders.
This module provides an abstract base class that future Ecourts adapters must implement.
It intentionally performs no network access.
"""
from abc import ABC, abstractmethod

class EcourtsClientBase(ABC):
    """Abstract base for an eCourts client adapter.

    Implementations must provide a method to fetch a case by CNR and return
    a normalized dictionary (or raise an exception). No network calls are
    performed by this placeholder.
    """

    @abstractmethod
    def fetch_case_by_cnr(self, cnr: str):
        """Fetch a case by its CNR/CRN.

        Return value: a normalized dict (see ecourts.normalizer) or None if not found.
        Implementations must not modify the local DB.
        """
        raise NotImplementedError


class NullEcourtsClient(EcourtsClientBase):
    """A null client used for testing and as a placeholder.

    Calling fetch_case_by_cnr will raise NotImplementedError so callers know
    the adapter is not yet implemented.
    """

    def fetch_case_by_cnr(self, cnr: str):
        raise NotImplementedError("Ecourts client not implemented")

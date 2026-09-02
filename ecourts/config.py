"""
eCourts integration configuration.

Configuration is environment-driven so credentials and provider URLs are
never hard-coded in the repository. This module performs no network access.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EcourtsConfig:
    """Runtime configuration for the eCourts integration."""

    api_base_url: str | None = None
    api_key: str | None = None
    enabled: bool = False

    @classmethod
    def from_environment(cls) -> "EcourtsConfig":
        """Build configuration from environment variables."""
        base_url = os.getenv("ECOURTS_API_BASE_URL")
        api_key = os.getenv("ECOURTS_API_KEY")

        enabled_value = os.getenv("ECOURTS_SYNC_ENABLED", "false").strip().casefold()
        enabled = enabled_value in {"1", "true", "yes", "on"}

        return cls(
            api_base_url=base_url.strip() if base_url and base_url.strip() else None,
            api_key=api_key if api_key else None,
            enabled=enabled,
        )

    def validate_for_api(self) -> None:
        """
        Validate configuration before an API sync is explicitly enabled.

        This does not make a network request.
        """
        if not self.enabled:
            return

        if not self.api_base_url:
            raise ValueError("ECOURTS_API_BASE_URL is required when eCourts sync is enabled")

        if not self.api_key:
            raise ValueError("ECOURTS_API_KEY is required when eCourts sync is enabled")


def get_ecourts_config() -> EcourtsConfig:
    """Return the current environment-based eCourts configuration."""
    return EcourtsConfig.from_environment()

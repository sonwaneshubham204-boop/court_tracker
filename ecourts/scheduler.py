"""
Scheduler-ready eCourts synchronization hook.

This module does not start a background scheduler by itself. It exposes one
safe function that a future scheduler (cron, APScheduler, Celery, etc.) can
call. Automatic synchronization remains disabled unless explicitly enabled
through eCourts environment configuration.
"""

from typing import Any

from ecourts.config import EcourtsConfig, get_ecourts_config
from ecourts.orchestrator import EcourtsSyncOrchestrator


def run_scheduled_sync(
    case_model: Any,
    orchestrator: EcourtsSyncOrchestrator,
    config: EcourtsConfig | None = None,
):
    """
    Execute one scheduled eCourts sync cycle when explicitly enabled.

    Returns None when synchronization is disabled. No provider/client call is
    made in that state.

    Configuration is validated before an enabled run. The actual sync work is
    delegated entirely to the existing orchestrator.
    """
    active_config = config or get_ecourts_config()

    if not active_config.enabled:
        return None

    active_config.validate_for_api()

    return orchestrator.run(case_model)

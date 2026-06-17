"""Source connector domain."""

from typing import Any, Mapping, Optional

from app.core import config


def stream_lookback_days(job_state: Optional[Mapping[str, Any]], last_synced_at) -> int:
    """How many days back to pull for one stream in a sync job.

    An explicit ``days`` in the job state (manual backfill button, Slack
    reconcile) overrides everything. Otherwise — the periodic Jira/GitHub
    re-sync path — a stream that has never synced gets a full backfill, and an
    already-synced stream gets the short re-sync window (dedup absorbs overlap).
    """
    explicit = (job_state or {}).get("days")
    if explicit:
        return int(explicit)
    if last_synced_at is None:
        return config.CONNECTOR_BACKFILL_DAYS
    return config.CONNECTOR_RESYNC_WINDOW_DAYS

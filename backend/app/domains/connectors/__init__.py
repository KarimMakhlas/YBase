"""Source connector domain."""

from datetime import datetime, timezone
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


def slack_reconcile_days(last_sync_at, now: Optional[datetime] = None) -> int:
    """Lookback (days) for a periodic Slack reconcile. At least the configured
    SLACK_RECONCILE_WINDOW_DAYS, but widened to span the whole gap since the last
    successful sync — so an outage longer than that window can't leave a
    permanent hole in memory (Slack only retries webhook delivery for ~3 days).
    Capped at the backfill ceiling. ``now`` is injectable for tests."""
    base = config.SLACK_RECONCILE_WINDOW_DAYS
    if last_sync_at is None:
        return base
    now = now or datetime.now(timezone.utc)
    gap_days = (now - last_sync_at).days + 1  # +1 day buffer for partial days
    return max(base, min(gap_days, config.CONNECTOR_BACKFILL_DAYS))

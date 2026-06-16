"""Confidence/recency scoring for memory nodes.

A decision that was reaffirmed last week should outrank one that was reversed
a year ago — both in the UI and when retrieval has to choose which evidence
chunks make the context cap.
"""

from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

_STATUS_WEIGHT = {
    "decided": 1.0,
    "reaffirmed": 1.0,
    "revisited": 0.7,
    "proposed": 0.5,
    "reversed": 0.25,
    # questions
    "open": 0.8,
    "resolved": 0.6,
}

_HALF_LIFE_DAYS = 270.0  # recency halves roughly every 9 months


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def node_score(
    status: Optional[str],
    data: Optional[Dict[str, Any]],
    evidence_count: int = 1,
    fallback_date: Any = None,
) -> float:
    """0..1 score combining status, recency, and how much evidence backs it."""
    weight = _STATUS_WEIGHT.get(status or "", 0.6)
    d = data or {}
    when = _parse_date(d.get("last_revisited")) or _parse_date(d.get("date")) \
        or _parse_date(fallback_date)
    if when is None:
        recency = 0.5
    else:
        age_days = max(0.0, (datetime.now(timezone.utc).date() - when).days)
        recency = 0.5 ** (age_days / _HALF_LIFE_DAYS)
    evidence = min(1.0, 0.5 + 0.125 * max(0, evidence_count - 1))
    return round(weight * (0.55 + 0.45 * recency) * evidence, 3)

"""Small shared helpers."""

from datetime import datetime
from typing import Optional


def iso_date(value: Optional[datetime]) -> Optional[str]:
    """A timestamptz column rendered as an ISO date (YYYY-MM-DD), or None."""
    return value.date().isoformat() if value else None

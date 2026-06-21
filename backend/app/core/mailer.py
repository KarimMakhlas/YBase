"""Outbound email via Resend. A logged no-op until RESEND_API_KEY +
DIGEST_FROM_EMAIL are set, so every caller works locally and gains real
delivery from a config change alone.
"""

import logging
from typing import Any, Dict, List

import httpx

from . import config

log = logging.getLogger("ybase.email")


def configured() -> bool:
    return bool(config.RESEND_API_KEY and config.DIGEST_FROM_EMAIL)


async def send(to: List[str], subject: str, text: str) -> Dict[str, Any]:
    """Send a plain-text email to recipients. Never raises — returns a result
    dict so callers can record delivery status without try/except everywhere."""
    recipients = [r for r in to if r]
    if not configured():
        return {"status": "skipped", "reason": "no email provider configured"}
    if not recipients:
        return {"status": "skipped", "reason": "no recipients"}
    try:
        async with httpx.AsyncClient(timeout=20) as cx:
            res = await cx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                json={
                    "from": config.DIGEST_FROM_EMAIL,
                    "to": recipients,
                    "subject": subject,
                    "text": text,
                },
            )
        res.raise_for_status()
        return {"status": "sent", "recipients": len(recipients)}
    except Exception as e:
        log.warning("email send failed (%s): %s", subject, e)
        return {"status": "error", "error": str(e)[:200]}

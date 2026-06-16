"""Symmetric encryption for connector secrets at rest (OAuth tokens).

Shared by every source connector (Slack, Jira, …) so the Fernet key is derived
once and tokens never sit in the database in plaintext.
"""

import base64
import hashlib

from cryptography.fernet import Fernet
from fastapi import HTTPException

from . import config


def _fernet() -> Fernet:
    if not config.CONNECTOR_SECRET_KEY:
        raise HTTPException(400, "CONNECTOR_SECRET_KEY is required for connector secrets")
    key = base64.urlsafe_b64encode(
        hashlib.sha256(config.CONNECTOR_SECRET_KEY.encode()).digest()
    )
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()

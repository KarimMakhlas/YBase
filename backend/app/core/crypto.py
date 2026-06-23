"""Symmetric encryption for connector secrets at rest (OAuth tokens).

Shared by every source connector (Slack, Jira, …) so tokens never sit in the
database in plaintext.

Key rotation: new ciphertext is tagged with a short, non-secret id of the key
that produced it ("<keyid>:<token>"). Decryption tries the matching key first,
then every other configured key (which also transparently handles legacy,
untagged tokens written before tagging existed). To rotate:
  1. set the new value in CONNECTOR_SECRET_KEY,
  2. move the previous value into CONNECTOR_SECRET_KEYS_OLD,
  3. run scripts/rotate_connector_key.py to re-encrypt everything,
  4. clear CONNECTOR_SECRET_KEYS_OLD.
"""

import base64
import hashlib
from typing import List

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from . import config


def _fernet_key(secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def _key_id(secret: str) -> str:
    """Short, stable, non-secret id used to tag ciphertext with its key."""
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def _primary() -> str:
    if not config.CONNECTOR_SECRET_KEY:
        raise HTTPException(400, "CONNECTOR_SECRET_KEY is required for connector secrets")
    return config.CONNECTOR_SECRET_KEY


def _keyring() -> List[str]:
    """Primary key first (used to encrypt new secrets), then any old keys kept
    for a rotation window (used to decrypt only)."""
    ring = [config.CONNECTOR_SECRET_KEY] if config.CONNECTOR_SECRET_KEY else []
    return ring + config.CONNECTOR_SECRET_KEYS_OLD


def encrypt_secret(value: str) -> str:
    secret = _primary()
    token = Fernet(_fernet_key(secret)).encrypt(value.encode()).decode()
    return f"{_key_id(secret)}:{token}"


def decrypt_secret(value: str) -> str:
    ring = _keyring()
    if not ring:
        raise HTTPException(400, "CONNECTOR_SECRET_KEY is required for connector secrets")
    # Fernet tokens are urlsafe-base64 (no ':'), so a ':' marks our key tag.
    if ":" in value:
        keyid, _, token = value.partition(":")
    else:  # legacy token, written before key tagging existed
        keyid, token = "", value
    # Matching key first, then all others — covers legacy/untagged tokens and a
    # tag whose key has since been removed from the front of the ring.
    ordered = [s for s in ring if _key_id(s) == keyid] + ring
    for secret in ordered:
        try:
            return Fernet(_fernet_key(secret)).decrypt(token.encode()).decode()
        except InvalidToken:
            continue
    raise HTTPException(400, "could not decrypt connector secret with any configured key")

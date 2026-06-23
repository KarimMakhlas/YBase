"""Re-encrypt all connector OAuth tokens with the current primary key.

Run after a key rotation:
  1. set the NEW key in CONNECTOR_SECRET_KEY,
  2. put the PREVIOUS key in CONNECTOR_SECRET_KEYS_OLD (so existing tokens still
     decrypt),
  3. run this script — it decrypts each token with whichever ring key matches
     and re-encrypts it tagged with the new key,
  4. then clear CONNECTOR_SECRET_KEYS_OLD.

Usage (from backend/):  python -m scripts.rotate_connector_key
"""

import asyncio

from app.core import db
from app.core.crypto import decrypt_secret, encrypt_secret


async def main() -> None:
    pool = await db.get_pool()
    changed = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, access_token_enc, refresh_token_enc FROM source_connections"
        )
        for r in rows:
            access = r["access_token_enc"]
            refresh = r["refresh_token_enc"]
            new_access = encrypt_secret(decrypt_secret(access)) if access else None
            new_refresh = encrypt_secret(decrypt_secret(refresh)) if refresh else None
            await conn.execute(
                "UPDATE source_connections SET access_token_enc=$2, refresh_token_enc=$3, "
                "updated_at=now() WHERE id=$1",
                r["id"], new_access, new_refresh,
            )
            changed += 1
    print(f"re-encrypted {changed} connection(s) with the current CONNECTOR_SECRET_KEY")
    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())

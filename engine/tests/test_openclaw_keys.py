"""OpenClaw gateway key minting."""

from __future__ import annotations

import hashlib

from app.services.openclaw_keys import mint_openclaw_gateway_key


def test_mint_openclaw_gateway_key_hashes_raw_value() -> None:
    raw, hashed = mint_openclaw_gateway_key()
    assert raw.startswith("oc-")
    assert hashed == hashlib.sha256(raw.encode()).hexdigest()
    other_raw, other_hashed = mint_openclaw_gateway_key()
    assert raw != other_raw
    assert hashed != other_hashed

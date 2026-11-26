"""Fleet-wide ceiling on live personal-bridge leases, shared by the WhatsApp and Zalo claims.

A claim stamps ``claim_token`` and a 30 s ``lease_until`` that its child renews about every second, so the rows holding
an unexpired lease are exactly the bridges alive across every engine process. A lease left by a dead process stops
counting when it expires, even though its row stays claimed until its teardown.

``admit`` runs inside the claim's transaction behind one transaction-scoped advisory lock per bridge (``LOCK_KEYS``,
hashed with ``hashtextextended`` like the engine's other advisory locks and sharing no prefix with theirs), so concurrent
claimers on any number of connections count and commit one at a time and the ceiling holds with no process-local
counter. The count is served by the partial indexes ``ix_whatsapp_personal_live_leases`` and
``ix_zalo_personal_live_leases``.
"""

from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel

from app.db.connection import DbConnection

type Bridge = Literal["whatsapp", "zalo"]

LOCK_KEYS: Final[Mapping[Bridge, str]] = {
    "whatsapp": "personal_bridge_fleet:whatsapp",
    "zalo": "personal_bridge_fleet:zalo",
}
_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0))"
_LIVE_SQL: Final[Mapping[Bridge, str]] = {
    "whatsapp": "SELECT count(*) AS live FROM whatsapp_personal_connections "
    "WHERE claim_token IS NOT NULL AND lease_until>clock_timestamp()",
    "zalo": "SELECT count(*) AS live FROM zalo_personal_connections "
    "WHERE claim_token IS NOT NULL AND lease_until>clock_timestamp()",
}


class _Live(BaseModel):
    live: int


class FleetFullError(Exception):
    """The fleet already holds ``ceiling`` live leases. A refusal, not a failure: the claim wrote nothing."""

    def __init__(self, live: int, ceiling: int) -> None:
        super().__init__(f"{live} live personal bridge leases at ceiling {ceiling}")
        self.live: int = live
        self.ceiling: int = ceiling


async def admit(db: DbConnection, bridge: Bridge, ceiling: int) -> None:
    """Hold the bridge's fleet lock until the caller's transaction ends, then refuse at the ceiling.

    The caller stamps its own lease in the same transaction, after this returns, so the next claimer counts it.
    """
    await db.execute(_LOCK_SQL, {"key": LOCK_KEYS[bridge]})
    live = _Live.model_validate(await db.fetchone(_LIVE_SQL[bridge])).live
    if live >= ceiling:
        raise FleetFullError(live, ceiling)

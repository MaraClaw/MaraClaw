"""Plan optional typing pulses; the child owner performs provider I/O.

The SDK has no stop-typing API: retiring work only stops future pulses.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime

from anyio import Lock
from fastapi import HTTPException
from psycopg import Error as PsycopgError
from psycopg_pool import PoolTimeout

from app.config import get_settings
from app.core.logging import logger
from app.dao.zalo_personal_dao import fenced
from app.db.errors import DbError
from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloConnection, ZaloRoute
from app.services.channels.sender_links import is_bound
from app.services.channels.zalo_personal_journal import route_matches


async def busy_peers(current: ZaloConnection) -> list[str]:
    """Read at most eight current, bound private reply destinations under fresh fences."""
    async with connection_ctx() as db:
        fresh = await fenced(current, connected=True)
        if fresh.account_id is None or fresh.account_id != current.account_id:
            return []
        prefix = (
            f"zalo_personal:1:{fresh.config_id.hex}:{fresh.auth_epoch.hex}:{fresh.generation.hex}:{fresh.account_id}:"
        )
        rows = await db.fetchall(
            "SELECT s.external_conv_id FROM gateway_report_deliveries d "
            "JOIN gateway_messages g ON g.id=d.message_id "
            "JOIN chat_sessions s ON s.id=d.destination_id AND s.agent_id=g.agent_id "
            "JOIN channel_sender_bindings b ON b.config_id=%(config)s AND b.provider='zalo' "
            "AND b.provider_account_id=%(account)s AND b.tenant_id=%(tenant)s "
            "AND s.external_conv_id=%(prefix)s || b.external_user_id "
            "WHERE d.destination_kind='im' AND d.delivered_at IS NULL AND d.terminal_at IS NULL "
            "AND d.tenant_id=%(tenant)s AND g.status IN ('pending','delivered') AND g.agent_id=%(agent)s "
            "AND g.created_at>=now()-interval '10 minutes' AND g.created_at<=now() "
            "AND NOT s.is_group AND starts_with(s.external_conv_id,%(prefix)s) "
            "AND b.external_user_id ~ '^[0-9]{1,64}$' AND b.external_user_id<>%(account)s "
            "AND length(s.external_conv_id)<=200 "
            "GROUP BY s.external_conv_id ORDER BY min(g.created_at),s.external_conv_id LIMIT 8",
            {
                "config": fresh.config_id,
                "account": fresh.account_id,
                "tenant": fresh.tenant_id,
                "agent": fresh.agent_id,
                "prefix": prefix,
            },
        )
        peers: list[str] = []
        for row in rows:
            route = ZaloRoute.from_external_id(str(row["external_conv_id"]))
            if (
                route_matches(fresh, route)
                and route.peer_id != fresh.account_id
                and await is_bound(route.config_id, route.account_id, route.peer_id)
            ):
                peers.append(route.peer_id)
        return peers


class TypingPump:
    """Track pulse times for the current bounded set of active peers.

    Construct one pump per owned child and await due(current) on its tick.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock: Callable[[], float] = clock
        self._last_pulse: dict[str, float] = {}
        self._lock: Lock = Lock()

    async def due(self, current: ZaloConnection) -> list[str]:
        """Return authorized busy peers due a pulse, at least five seconds apart."""
        async with self._lock:
            if (
                not get_settings().ZALO_PERSONAL_TYPING_ENABLED
                or current.state != "connected"
                or not current.desired
                or current.claim_token is None
                or current.account_id is None
                or current.lease_until is None
                or current.lease_until <= datetime.now(UTC)
            ):
                self._last_pulse.clear()
                return []
            try:
                peers = await busy_peers(current)
            except HTTPException as exc:
                logger.debug("Zalo typing authorization refused status={}", exc.status_code)
                self._last_pulse.clear()
                return []
            except (DbError, PsycopgError, PoolTimeout) as exc:
                logger.warning("Zalo typing planning failed error_type={}", type(exc).__name__)
                return []
            now = self._clock()
            self._last_pulse = {peer: sent for peer, sent in self._last_pulse.items() if peer in peers}
            due = [peer for peer in peers if peer not in self._last_pulse or now - self._last_pulse[peer] >= 5]
            self._last_pulse.update((peer, now) for peer in due)
            return due

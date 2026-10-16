"""ACK evidence invalidated inside redis-py's transparent reconnect boundary."""

from typing import override

from redis.asyncio.client import PubSub
from redis.asyncio.connection import Connection


class AcknowledgedPubSub(PubSub):
    acknowledged: bool = False

    @override
    async def check_health(self) -> None:
        if self.connection is None or not self.connection.is_connected:
            self.acknowledged = False
        await super().check_health()

    @override
    async def on_connect(self, connection: Connection) -> None:
        self.acknowledged = False
        await super().on_connect(connection)

    @override
    async def _reconnect(
        self,
        conn: Connection,
        error: BaseException | None = None,
        failure_count: int | None = None,
        start_time: float | None = None,
        command_name: str | None = None,
    ) -> None:
        # redis-py retries reads internally: clearing only in the caller is too late.
        self.acknowledged = False
        await super()._reconnect(conn, error, failure_count, start_time, command_name)

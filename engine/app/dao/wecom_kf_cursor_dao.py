"""PostgreSQL checkpoint for WeCom customer-service message pulls."""

from dataclasses import dataclass
from typing import final
from uuid import UUID

from app.core.json_types import str_from_row
from app.db.session import connection_ctx


@dataclass(frozen=True, slots=True)
class WeComKFCursorKey:
    config_id: UUID
    corp_id: str
    open_kfid: str


_KEY = "channel_config_id = %(config)s AND corp_id = %(corp)s AND open_kfid = %(kf)s"
_CUTOFF = "statement_timestamp() - INTERVAL '3 days'"


@final
class WeComKFCursorDAO:
    async def read(self, key: WeComKFCursorKey) -> str | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                f"SELECT cursor FROM wecom_kf_cursors WHERE {_KEY} AND updated_at >= {_CUTOFF}",
                {"config": key.config_id, "corp": key.corp_id, "kf": key.open_kfid},
            )
        return str_from_row(row["cursor"]) if row is not None else None

    async def compare_and_set(self, key: WeComKFCursorKey, expected: str | None, cursor: str) -> bool:
        params = {"config": key.config_id, "corp": key.corp_id, "kf": key.open_kfid, "old": expected, "new": cursor}
        async with connection_ctx() as db:
            if expected is not None:
                return (
                    await db.fetchone(
                        "UPDATE wecom_kf_cursors SET cursor = %(new)s, updated_at = statement_timestamp() "
                        f"WHERE {_KEY} AND updated_at >= {_CUTOFF} AND cursor IS NOT DISTINCT FROM %(old)s RETURNING cursor",
                        params,
                    )
                    is not None
                )
            # An expired cursor counts as absent, but a peer may have replaced it since our read.
            return (
                await db.fetchone(
                    "INSERT INTO wecom_kf_cursors (channel_config_id, corp_id, open_kfid, cursor, updated_at) "
                    "VALUES (%(config)s, %(corp)s, %(kf)s, %(new)s, statement_timestamp()) "
                    "ON CONFLICT (channel_config_id, corp_id, open_kfid) DO UPDATE "
                    "SET cursor = EXCLUDED.cursor, updated_at = EXCLUDED.updated_at "
                    f"WHERE (CASE WHEN wecom_kf_cursors.updated_at >= {_CUTOFF} THEN wecom_kf_cursors.cursor END) "
                    "IS NOT DISTINCT FROM %(old)s::text RETURNING cursor",
                    params,
                )
                is not None
            )


wecom_kf_cursor_dao = WeComKFCursorDAO()

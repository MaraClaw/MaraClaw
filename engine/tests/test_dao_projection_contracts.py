from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Self, assert_type
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao
from app.dao.projection_contracts import AgentNameAvatarProjection, UserNameAvatarProjection
from app.dao.user_dao import user_dao
from app.db import pool as pool_module, session as session_module
from app.db.connection import Params, Row


class _FakeCursor:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self.raw: _FakeRawConnection = raw

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Params = None) -> None:
        assert isinstance(params, Mapping)
        self.raw.executed.append((" ".join(query.split()), dict(params)))

    async def fetchone(self) -> Row | None:
        return self.raw.rows[0] if self.raw.rows else None

    async def fetchall(self) -> list[Row]:
        return self.raw.rows.copy()


class _FakeRawConnection:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.executed: list[tuple[str, Mapping[str, object]]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _PoolConnectionCM:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self.raw: _FakeRawConnection = raw

    async def __aenter__(self) -> _FakeRawConnection:
        return self.raw

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _FakePool:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self.raw: _FakeRawConnection = raw

    def connection(self) -> _PoolConnectionCM:
        return _PoolConnectionCM(self.raw)


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeRawConnection]:
    connection = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(connection))
    token = session_module._conn_ctx.set(None)
    try:
        yield connection
    finally:
        session_module._conn_ctx.reset(token)
        pool_module.reset_pool_for_tests()


@pytest.mark.parametrize("avatar_url", [None, "", "https://example.test/agent.png"])
async def test_agent_projection_preserves_native_uuid_name_and_nullable_avatar(
    raw: _FakeRawConnection, avatar_url: str | None
) -> None:
    tenant_id, agent_id = UUID(int=10), UUID(int=11)
    raw.rows = [
        {"id": agent_id, "name": "Research Assistant", "avatar_url": avatar_url},
        {"id": UUID(int=12), "name": "", "avatar_url": None},
    ]

    result = await agent_dao.list_id_name_avatar_active_nonsystem(tenant_id)

    assert_type(result, Sequence[AgentNameAvatarProjection])
    assert result == raw.rows
    assert result[0]["id"] is agent_id
    assert TypeAdapter(list[AgentNameAvatarProjection]).validate_python(result, strict=True) == raw.rows
    assert raw.executed == [
        (
            (
                "SELECT id, name, avatar_url FROM agents WHERE tenant_id = %(tenant_id)s AND is_system IS FALSE "
                "AND NOT (status = ANY(%(exclude_statuses)s))"
            ),
            {"tenant_id": tenant_id, "exclude_statuses": ["stopped", "error"]},
        )
    ]


@pytest.mark.parametrize(
    ("display_name", "avatar_url"),
    [("Display Name", ""), ("member@example.test", None), ("User", "https://example.test/user.png")],
)
async def test_user_projection_preserves_sql_selected_display_name_and_avatar(
    raw: _FakeRawConnection, display_name: str, avatar_url: str | None
) -> None:
    tenant_id, user_id = UUID(int=20), UUID(int=21)
    raw.rows = [{"id": user_id, "display_name": display_name, "avatar_url": avatar_url}]

    result = await user_dao.list_id_name_avatar_for_tenant(tenant_id)

    assert_type(result, Sequence[UserNameAvatarProjection])
    assert result == raw.rows
    assert result[0]["id"] is user_id
    assert TypeAdapter(list[UserNameAvatarProjection]).validate_python(result, strict=True) == raw.rows
    assert raw.executed == [
        (
            (
                "SELECT u.id, COALESCE(NULLIF(u.display_name, ''), NULLIF(i.email, ''), 'User') AS display_name, "
                "u.avatar_url FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                "WHERE u.tenant_id = %(tenant_id)s"
            ),
            {"tenant_id": tenant_id},
        )
    ]


async def test_agent_projection_returns_empty_list_without_matching_rows(raw: _FakeRawConnection) -> None:
    result = await agent_dao.list_id_name_avatar_active_nonsystem(UUID(int=30))

    assert result == []
    assert len(raw.executed) == 1


async def test_user_projection_returns_empty_list_without_matching_rows(raw: _FakeRawConnection) -> None:
    result = await user_dao.list_id_name_avatar_for_tenant(UUID(int=40))

    assert result == []
    assert len(raw.executed) == 1


@pytest.mark.parametrize(
    "last_at",
    [
        datetime(2026, 10, 21, 10, 11, 12, 345678, tzinfo=UTC),
        datetime(2026, 10, 21, 10, 11, 12, 345678, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        None,
    ],
)
async def test_conversation_stats_preserve_count_and_native_timestamp(
    raw: _FakeRawConnection, last_at: datetime | None
) -> None:
    conversation_id = "conversation_'quoted"
    raw.rows = [{"cnt": 7, "last_at": last_at}]

    result = await chat_message_dao.message_stats_for_conversation(conversation_id)

    assert_type(result, tuple[int, datetime | None])
    assert result == (7, last_at)
    assert result[1] is last_at
    assert raw.executed == [
        (
            (
                "SELECT COUNT(*) AS cnt, MAX(created_at) AS last_at "
                "FROM chat_messages WHERE conversation_id = %(conversation_id)s"
            ),
            {"conversation_id": conversation_id},
        )
    ]


@pytest.mark.parametrize("row", [None, {}, {"cnt": 0, "last_at": None}, {"cnt": None}])
async def test_conversation_stats_keep_empty_and_missing_cell_defaults(
    raw: _FakeRawConnection, row: Row | None
) -> None:
    raw.rows = [] if row is None else [row]

    result = await chat_message_dao.message_stats_for_conversation("empty")

    assert result == (0, None)
    assert len(raw.executed) == 1

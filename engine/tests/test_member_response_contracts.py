import base64
import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import ResponseValidationError
from pydantic import TypeAdapter

from app.api import activity as activity_api, notification as notification_api
from app.core.domain.activity import ActivityAction
from app.core.notification_kinds import NotificationKind
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao.activity_log_dao import ActivityTimelineEntry, ActivityTimelineQuery
from app.records.activity_log import AgentActivityLogRecord
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.notification import NotificationRecord
from app.records.user import UserRecord
from app.schemas.activity_responses import AgentActivityCursor, ConversationMessageResponse

AGENT_ID = UUID("00000000-0000-0000-0000-000000000001")
STAMP = datetime(2026, 10, 20, 12, tzinfo=UTC)
ROWS = TypeAdapter(list[dict[str, object]])


@pytest.fixture
def member() -> UserRecord:
    return UserRecord(id=uuid4(), identity_id=None, tenant_id=uuid4(), display_name="Member")


@pytest.fixture
def app(member: UserRecord, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    application = FastAPI()
    application.include_router(activity_api.router, prefix="/api")
    application.include_router(notification_api.router, prefix="/api")
    application.dependency_overrides[get_current_user] = lambda: member
    agent = AgentRecord(id=AGENT_ID, name="Agent", creator_id=member.id)
    monkeypatch.setattr(activity_api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as transport:
        yield transport


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Callable[[object, str, object], AsyncMock]:
    def returns(target: object, method: str, result: object) -> AsyncMock:
        mocked = AsyncMock(return_value=result)
        monkeypatch.setattr(target, method, mocked)
        return mocked

    return returns


@pytest.mark.parametrize("populated", [False, True])
async def test_notification_list_preserves_nullable_and_populated_fields(client, member, stub, populated):
    notification = NotificationRecord(
        id=uuid4(),
        type=NotificationKind.SYSTEM,
        title="Title",
        body="Body",
        user_id=member.id,
        link="/app" if populated else None,
        ref_id=AGENT_ID if populated else None,
        sender_name="Sender" if populated else None,
        is_read=populated,
        created_at=STAMP if populated else None,
    )
    listing = stub(notification_api.notification_dao, "list_for_user", [notification])
    response = await client.get("/api/notifications?category=social&limit=2&offset=3&unread_only=true")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(notification.id),
            "type": "system",
            "title": "Title",
            "body": "Body",
            "link": "/app" if populated else None,
            "ref_id": str(AGENT_ID) if populated else None,
            "sender_name": "Sender" if populated else None,
            "is_read": populated,
            "archived_at": None,
            "created_at": STAMP.isoformat() if populated else None,
        },
    ]
    listing.assert_awaited_once_with(
        member.id,
        unread_only=True,
        types=[
            NotificationKind.PLAZA_COMMENT,
            NotificationKind.PLAZA_REPLY,
            NotificationKind.MENTION,
            NotificationKind.BROADCAST,
        ],
        limit=2,
        offset=3,
        view="active",
    )


@pytest.mark.parametrize("category", ["tool", "all"])
async def test_unread_count_is_scoped_to_member_and_category(client, member, stub, category):
    counter = stub(notification_api.notification_dao, "count_unread_for_user", 7)
    response = await client.get(f"/api/notifications/unread-count?category={category}")
    assert response.status_code == 200
    assert response.json() == {"unread_count": 7}
    counter.assert_awaited_once_with(member.id, types=[NotificationKind.AUTONOMY_L2] if category == "tool" else None)


@pytest.mark.parametrize(("all_read", "kwargs"), [(False, {}), (True, {"types": None})])
async def test_read_acknowledgement_preserves_member_scope(client, member, stub, all_read, kwargs):
    notification_id = uuid4()
    method = "mark_all_read_for_user" if all_read else "mark_read_for_user"
    marker = stub(notification_api.notification_dao, method, None)
    path = "read-all" if all_read else f"{notification_id}/read"
    response = await client.post(f"/api/notifications/{path}")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    marker.assert_awaited_once_with(*((member.id,) if all_read else (notification_id, member.id)), **kwargs)


@pytest.mark.parametrize("populated", [False, True])
async def test_activity_detail_keeps_dynamic_values_and_nullable_fields(client, stub, populated):
    related_id = uuid4()
    log = AgentActivityLogRecord(
        id=uuid4(),
        agent_id=AGENT_ID,
        action_type=ActivityAction.TOOL_CALL,
        summary="Summary",
        detail_json={"nested": [None, {"timestamp": STAMP, "id": related_id}]},
        related_id=related_id if populated else None,
        created_at=STAMP if populated else None,
    )
    listing = stub(activity_api.agent_activity_log_dao, "list_for_agent", [log])
    response = await client.get(f"/api/agents/{AGENT_ID}/activity?limit=2")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(log.id),
            "action_type": "tool_call",
            "summary": "Summary",
            "detail": {"nested": [None, {"timestamp": "2026-10-20T12:00:00+00:00", "id": str(related_id)}]},
            "related_id": str(related_id) if populated else None,
            "created_at": STAMP.isoformat() if populated else None,
        },
    ]
    listing.assert_awaited_once_with(AGENT_ID, limit=2)


def _entry(created_at: datetime | None, log_id: UUID | None = None, summary: str = "Summary") -> ActivityTimelineEntry:
    return ActivityTimelineEntry(
        id=log_id or uuid4(), action_type=ActivityAction.TOOL_CALL, summary=summary, created_at=created_at
    )


async def test_activity_page_returns_only_projection_fields_and_fresh_manage_check(client, member, stub, monkeypatch):
    entry = _entry(STAMP)
    undated = _entry(None)
    access = AsyncMock(return_value=(AgentRecord(id=AGENT_ID, name="Agent", creator_id=member.id), "manage"))
    monkeypatch.setattr(activity_api, "check_agent_access", access)
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [entry, undated])
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page?limit=1&action_type=tool_call")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [
        {"id": str(entry.id), "action_type": "tool_call", "summary": "Summary", "created_at": STAMP.isoformat()}
    ]
    cursor = AgentActivityCursor.decode(body["next_cursor"])
    assert (cursor.agent_id, cursor.action_type, cursor.created_at, cursor.id) == (
        AGENT_ID,
        ActivityAction.TOOL_CALL,
        STAMP,
        entry.id,
    )
    listing.assert_awaited_once_with(
        ActivityTimelineQuery(agent_id=AGENT_ID, limit=2, action_type=ActivityAction.TOOL_CALL, before=None)
    )
    access.assert_awaited_once_with(member, AGENT_ID, fresh=True)


async def test_activity_page_last_page_has_no_cursor_and_null_timestamp_is_preserved(client, stub):
    entry = _entry(None)
    stub(activity_api.agent_activity_log_dao, "list_timeline_page", [entry])
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page")
    assert response.status_code == 200
    assert response.json() == {
        "items": [{"id": str(entry.id), "action_type": "tool_call", "summary": "Summary", "created_at": None}],
        "next_cursor": None,
    }


async def test_activity_page_denies_use_access_before_query(client, stub, monkeypatch):
    agent = AgentRecord(id=AGENT_ID, name="Agent", creator_id=uuid4())
    monkeypatch.setattr(activity_api, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [])
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page")
    assert response.status_code == 403
    listing.assert_not_awaited()


async def test_activity_page_rechecks_access_on_later_page_after_revocation(client, stub, monkeypatch):
    stub(activity_api.agent_activity_log_dao, "list_timeline_page", [_entry(STAMP), _entry(STAMP)])
    first = await client.get(f"/api/agents/{AGENT_ID}/activity/page?limit=1")
    cursor = first.json()["next_cursor"]
    revoked = AsyncMock(side_effect=HTTPException(403, "Forbidden"))
    monkeypatch.setattr(activity_api, "check_agent_access", revoked)
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [])
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page", params={"cursor": cursor})
    assert response.status_code == 403
    revoked.assert_awaited_once()
    listing.assert_not_awaited()


@pytest.mark.parametrize("limit", [0, -1, 201, "invalid"])
async def test_activity_page_rejects_invalid_limit(client, limit):
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page", params={"limit": limit})
    assert response.status_code == 422


async def test_activity_page_accepts_maximum_limit(client, stub):
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [])
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page", params={"limit": 200})
    assert response.status_code == 200
    assert listing.await_args.args[0].limit == 201


async def test_activity_page_accepts_unknown_exact_action_type(client, stub):
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [])
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page", params={"action_type": "future_action"})
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    assert listing.await_args.args[0].action_type == "future_action"


def _cursor(**fault: object) -> str:
    payload = {
        "version": 1,
        "agent_id": str(AGENT_ID),
        "action_type": None,
        "created_at": STAMP.isoformat(),
        "id": str(uuid4()),
        **fault,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "!not-base64!",
        "e30",
        "bnVsbA",
        "x" * 2049,
        _cursor(version=2),
        _cursor(created_at="2026-10-20T12:00:00"),
        _cursor(id="invalid"),
        _cursor(extra="invalid"),
    ],
)
async def test_activity_page_rejects_malformed_cursor(client, stub, cursor):
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [])
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page", params={"cursor": cursor})
    assert response.status_code == 422
    listing.assert_not_awaited()


async def test_activity_page_rejects_empty_cursor(client):
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page", params={"cursor": ""})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("bound", "requested"),
    [
        ({"agent_id": str(uuid4())}, {}),
        ({"action_type": "tool_call"}, {}),
        ({}, {"action_type": "tool_call"}),
        ({"action_type": "error"}, {"action_type": "tool_call"}),
        ({"action_type": "future_action"}, {"action_type": "future_action_other"}),
    ],
)
async def test_activity_page_cursor_is_bound_to_agent_and_filter(client, stub, bound, requested):
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [])
    response = await client.get(
        f"/api/agents/{AGENT_ID}/activity/page", params={"cursor": _cursor(**bound), **requested}
    )
    assert response.status_code == 422
    listing.assert_not_awaited()


async def test_activity_page_cursor_accepts_matching_unknown_action_type(client, stub):
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [])
    cursor = _cursor(action_type="future_action")
    response = await client.get(
        f"/api/agents/{AGENT_ID}/activity/page", params={"cursor": cursor, "action_type": "future_action"}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    assert listing.await_args.args[0].action_type == "future_action"


@pytest.mark.parametrize("created_at", [STAMP, None])
async def test_activity_page_cursor_seeks_from_bound_position(client, stub, created_at):
    log_id = uuid4()
    listing = stub(activity_api.agent_activity_log_dao, "list_timeline_page", [])
    cursor = _cursor(created_at=created_at.isoformat() if created_at else None, id=str(log_id))
    response = await client.get(f"/api/agents/{AGENT_ID}/activity/page", params={"cursor": cursor})
    assert response.status_code == 200
    listing.assert_awaited_once_with(
        ActivityTimelineQuery(agent_id=AGENT_ID, limit=51, action_type=None, before=(created_at, log_id))
    )


@pytest.mark.parametrize("suffix", ["activity/page"])
async def test_activity_page_enforces_access_and_authentication(app, client, member, monkeypatch, suffix):
    member.is_active = False
    monkeypatch.setattr(activity_api, "check_agent_access", check_agent_access)
    assert (await client.get(f"/api/agents/{AGENT_ID}/{suffix}")).status_code == 403
    app.dependency_overrides.clear()
    assert (await client.get(f"/api/agents/{AGENT_ID}/{suffix}")).status_code == 401


@pytest.mark.parametrize(
    "conv_id",
    ["web_member", "feishu_p2p_member", "slack_dm_member", "discord_room_member", "agent_peer", str(AGENT_ID)],
)
async def test_conversation_messages_preserve_sender_omission_and_open_roles(client, member, stub, conv_id):
    peer = conv_id.startswith("agent_") or len(conv_id) == 36
    message = ChatMessageRecord(
        id=uuid4(), agent_id=AGENT_ID, user_id=member.id, role="future_role", content="[发送者: Alice (ID:1)] Hello"
    )
    messages = stub(
        activity_api.chat_message_dao, "list_for_session" if peer else "list_for_agent_conversation", [message]
    )
    response = await client.get(f"/api/agents/{AGENT_ID}/chat-history/{conv_id}?limit=2")
    expected = {
        "id": str(message.id),
        "role": "future_role",
        "content": message.content if peer else "Hello",
        "created_at": None,
    }
    if peer:
        expected["sender_name"] = "Unknown"
    assert response.status_code == 200
    assert response.json() == [expected]
    if peer:
        messages.assert_awaited_once_with(conversation_id=conv_id, limit=2)
    else:
        messages.assert_awaited_once_with(agent_id=AGENT_ID, conversation_id=conv_id, limit=2, ascending=True)


async def test_conversation_list_keeps_sorting_truncation_and_null_timestamp(client, member, monkeypatch, stub):
    session = ChatSessionRecord(id=uuid4(), agent_id=AGENT_ID, user_id=member.id, peer_agent_id=uuid4())
    groups = AsyncMock(
        side_effect=[
            [{"group_key": member.id, "cnt": 2, "last_at": None}],
            [{"group_key": "feishu_p2p_member", "cnt": 3, "last_at": STAMP}],
            [{"group_key": "slack_dm_member", "cnt": 4, "last_at": None}],
            [{"group_key": "discord_room_member", "cnt": 5, "last_at": None}],
        ]
    )
    monkeypatch.setattr(activity_api.chat_message_dao, "conversation_groups_for_agent", groups)
    stub(activity_api.user_dao, "display_names_for_ids", {member.id: "Member"})
    monkeypatch.setattr(
        activity_api.chat_message_dao,
        "latest_contents",
        AsyncMock(
            side_effect=[
                {str(member.id): "w" * 90},
                {"feishu_p2p_member": "f" * 90},
                {"feishu_p2p_member": "[发送者: Alice (ID:1)] First"},
                {"slack_dm_member": "s" * 90},
                {"discord_room_member": "d" * 90},
            ]
        ),
    )
    stub(activity_api.chat_session_dao, "list_agent_peer_sessions_for_agent", [session])
    stub(activity_api.agent_dao, "get_many", [])
    stub(
        activity_api.chat_message_dao, "message_stats_for_conversations", {str(session.id): (1, STAMP.replace(day=21))}
    )
    latest = ChatMessageRecord(id=uuid4(), agent_id=AGENT_ID, user_id=member.id, role="assistant", content="a" * 90)
    stub(activity_api.chat_message_dao, "list_latest_for_conversations", {str(session.id): [latest]})
    response = await client.get(f"/api/agents/{AGENT_ID}/chat-history/conversations")
    assert response.status_code == 200
    rows = ROWS.validate_json(response.content)
    assert [row["partner_type"] for row in rows] == ["agent", "feishu", "user", "slack", "discord"]
    assert [row["last_message"] for row in rows] == [letter * 80 for letter in "afwsd"]
    assert [row["message_count"] for row in rows] == [1, 3, 2, 4, 5]
    assert [row["last_at"] for row in rows] == [STAMP.replace(day=21).isoformat(), STAMP.isoformat(), None, None, None]
    assert rows[1]["partner_name"] == "📱 Alice"
    assert all(
        set(row)
        == {"conv_id", "partner_type", "partner_id", "partner_name", "last_message", "message_count", "last_at"}
        for row in rows
    )


@pytest.mark.parametrize("suffix", ["activity", "chat-history/conversations", "chat-history/web_member"])
async def test_activity_routes_enforce_agent_access(client, member, monkeypatch, suffix):
    member.is_active = False
    monkeypatch.setattr(activity_api, "check_agent_access", check_agent_access)
    response = await client.get(f"/api/agents/{AGENT_ID}/{suffix}")
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "notifications",
        "notifications/unread-count",
        f"agents/{AGENT_ID}/activity",
        f"agents/{AGENT_ID}/chat-history/conversations",
        f"agents/{AGENT_ID}/chat-history/web_member",
    ],
)
async def test_member_routes_require_authentication(app, client, path):
    app.dependency_overrides.clear()
    response = await client.get(f"/api/{path}")
    assert response.status_code == 401


async def test_unread_count_rejects_invalid_response_through_fastapi(client, stub):
    stub(notification_api.notification_dao, "count_unread_for_user", "invalid")
    with pytest.raises(ResponseValidationError) as error:
        await client.get("/api/notifications/unread-count")
    assert error.value.errors()[0]["loc"] == ("response", "unread_count")


@pytest.mark.parametrize("sender_present", [False, True])
def test_message_contract_distinguishes_omitted_and_null_sender(sender_present):
    payload: dict[str, object] = {"id": "message", "role": "future_role", "content": "", "created_at": None}
    if sender_present:
        payload["sender_name"] = None
    adapter = TypeAdapter(ConversationMessageResponse)
    result = adapter.validate_python(payload)
    assert isinstance(result, dict)
    assert adapter.dump_python(result) == payload

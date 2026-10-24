import base64
import json
from uuid import UUID, uuid4

import pytest

from app.records.chat_attachment import ChatAttachment
from app.services.chat_history import HistoryCursor

pytest_plugins = ["tests.chat_history_fakes"]


async def test_latest_page_is_cached_without_repeating_reads_or_projection(history_http):
    # Given a populated latest-page cache through the real HTTP router.
    h, client = history_http
    first = await client.get(h.url, params={"limit": 2})
    # When the same page is requested again.
    repeated = await client.get(h.url, params={"limit": 2})
    # Then its ascending wire result is reused, but freshness and read marks still run.
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert [entry["message_id"] for entry in repeated.json()["items"]] == [str(UUID(int=4)), str(UUID(int=5))]
    assert repeated.json()["has_more"] is True
    assert h.page_reads == h.projections == 1
    assert h.revision_reads == len(h.read_marks) == 2
    assert set(h.cache.expires.values()) == {30}


async def test_equal_timestamp_pages_exhaust_without_gaps_after_concurrent_append(history_http):
    # Given a latest boundary at full microsecond precision and an appended row with the same timestamp.
    h, client = history_http
    latest = (await client.get(h.url, params={"limit": 2})).json()
    h.rows.append(h.message(6))
    h.revision = str(uuid4())
    # When older pages follow the stable timestamp/UUID boundary.
    middle = (await client.get(h.url, params={"limit": 2, "cursor": latest["next_cursor"]})).json()
    oldest = (await client.get(h.url, params={"limit": 2, "cursor": middle["next_cursor"]})).json()
    fresh = (await client.get(h.url, params={"limit": 2})).json()
    # Then no tied row is skipped/duplicated, and only newest requests mark read.
    assert [[entry["message_id"] for entry in page["items"]] for page in (oldest, middle, latest)] == [
        [str(UUID(int=1))],
        [str(UUID(int=2)), str(UUID(int=3))],
        [str(UUID(int=4)), str(UUID(int=5))],
    ]
    assert oldest["next_cursor"] is None
    assert oldest["has_more"] is False
    assert [entry["message_id"] for entry in fresh["items"]] == [str(UUID(int=5)), str(UUID(int=6))]
    assert len(h.read_marks) == 2


@pytest.mark.parametrize(("denial", "status"), [("agent", 403), ("private", 403), ("missing", 404)])
async def test_authorization_precedes_populated_cache(history_http, denial, status):
    # Given a cached page and subsequently revoked authorization/session visibility.
    h, client = history_http
    assert (await client.get(h.url)).status_code == 200
    match denial:
        case "agent":
            h.allowed = False
        case "private":
            h.session.user_id = uuid4()
        case "missing":
            h.visible = False
    # When the cached page is requested with the now-unauthorized principal.
    response = await client.get(h.url)
    # Then cache contents cannot bypass any of the three authorization gates.
    assert response.status_code == status
    assert h.revision_reads == h.page_reads == 1


@pytest.mark.parametrize("invalid", ["", "!", "wrong-agent", "wrong-session", "version", "naive"])
async def test_invalid_or_foreign_cursors_are_rejected(history_http, invalid):
    # Given a malformed or out-of-scope opaque cursor.
    h, client = history_http
    cursor = HistoryCursor(agent_id=h.agent.id, session_id=h.session.id, created_at=h.timestamp, message_id=UUID(int=4))
    payload = cursor.model_dump(mode="json")
    match invalid:
        case "wrong-agent":
            payload["agent_id"] = str(uuid4())
        case "wrong-session":
            payload["session_id"] = str(uuid4())
        case "version":
            payload["version"] = 2
        case "naive":
            payload["created_at"] = "2026-10-24T12:00:00.123456"
    raw = invalid if invalid in ("", "!") else base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    # When it crosses the HTTP cursor boundary.
    response = await client.get(h.url, params={"cursor": raw})
    # Then it is rejected before history or cache reads.
    assert response.status_code == 400
    assert h.revision_reads == h.page_reads == 0


@pytest.mark.parametrize("limit", [0, 501])
async def test_limit_is_bounded_at_http_boundary(history_http, limit):
    # Given a limit outside the supported row range.
    h, client = history_http
    # When submitted through HTTP.
    response = await client.get(h.url, params={"limit": limit})
    # Then framework validation rejects it without reading history.
    assert response.status_code == 422
    assert h.page_reads == 0


async def test_inline_tools_stay_together_and_attachments_survive_cache_roundtrip(history_http):
    # Given one inline-tool row and one attachment row, with a row limit of one.
    h, client = history_http
    h.session.source_channel = "agent"
    inline = h.rows[-1]
    inline.participant_id = uuid4()
    inline.content = 'Before\n```tool_code\nlookup\n```\n```json\n{"q":"x"}\n```\nAfter'
    attached = h.rows[-2]
    attached.role = "user"
    attached.thinking = "reasoning"
    attached.client_submission_id = uuid4()
    attached.attachments = (ChatAttachment(path="uploads/report.txt", name="报告.txt"),)
    first = (await client.get(h.url, params={"limit": 1})).json()
    # When the inline page is cached and the attachment page is read and cached.
    repeated = (await client.get(h.url, params={"limit": 1})).json()
    older = (await client.get(h.url, params={"limit": 1, "cursor": first["next_cursor"]})).json()
    older_cached = (await client.get(h.url, params={"limit": 1, "cursor": first["next_cursor"]})).json()
    # Then all fan-out entries share their row and stable IDs, and projection metadata is preserved.
    assert repeated == first
    assert [entry["role"] for entry in first["items"]] == ["assistant", "tool_call", "assistant"]
    assert [entry["entry_id"] for entry in first["items"]] == [f"{inline.id}:{index}" for index in range(3)]
    assert first["items"][1]["toolArgs"] == {"q": "x"}
    assert all(entry["participant_id"] == str(inline.participant_id) for entry in first["items"])
    assert older == older_cached
    assert older["items"][0]["attachments"] == [{"path": "uploads/report.txt", "name": "报告.txt"}]
    assert older["items"][0]["client_submission_id"] == str(attached.client_submission_id)
    assert older["items"][0]["thinking"] == "reasoning"
    assert h.page_reads == h.projections == 2
    assert not h.read_marks


@pytest.mark.parametrize("failure", ["unavailable", "corrupt", "oversized"])
async def test_cache_failure_does_not_break_history(history_http, failure):
    # Given a failing, corrupt, or too-large response cache.
    h, client = history_http
    first = (await client.get(h.url)).json()
    match failure:
        case "unavailable":
            h.cache.fail = True
        case "corrupt":
            for key in h.cache.store:
                h.cache.store[key] = '{"items":false}'
        case "oversized":
            h.cache.store.clear()
            h.rows[-1].content = "x" * 70000
    # When history is requested again.
    response = await client.get(h.url)
    # Then SQL/projection serve the page and oversized values are not stored.
    assert response.status_code == 200
    assert h.page_reads == h.projections == 2
    if failure == "oversized":
        assert response.json()["items"][-1]["content"] == "x" * 70000
        assert not h.cache.store
    else:
        assert response.json() == first


async def test_empty_history_has_terminal_envelope(history_http):
    # Given a session without messages.
    h, client = history_http
    h.rows.clear()
    # When the default latest page is fetched.
    response = await client.get(h.url)
    # Then there is no synthetic continuation cursor.
    assert response.json() == {"items": [], "next_cursor": None, "has_more": False}


async def test_default_limit_and_page_identity_are_independent(history_http):
    # Given more than the default fifty rows.
    h, client = history_http
    h.rows = [h.message(index) for index in range(1, 61)]
    # When the latest page is requested with default and explicit small limits.
    default = (await client.get(h.url)).json()
    small = (await client.get(h.url, params={"limit": 1})).json()
    # Then limit is part of cache identity and the default remains fifty database rows.
    assert len(default["items"]) == 50
    assert len(small["items"]) == 1
    assert h.page_reads == h.projections == 2

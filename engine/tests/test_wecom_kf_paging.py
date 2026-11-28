"""Exercise the signed callback with a controlled provider and clock."""

import base64
import json
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from app.api import wecom
from app.config import get_settings
from app.core.json_types import JsonObject
from app.records.channel_config import ChannelConfigRecord
from app.services.channels import submissions
from app.services.channels.wecom_kf import budget_stop_count
from im_webhook_fixtures import channel_config, webhook_client


@dataclass
class Pull:
    client: httpx.AsyncClient
    config: ChannelConfigRecord
    accepted: list[str] = field(default_factory=list)
    sent: list[JsonObject] = field(default_factory=list)
    pages: list[JsonObject] = field(default_factory=list)
    checkpoints: list[tuple[str | None, str]] = field(default_factory=list)
    cursor: str | None = None
    now: float = 0
    lose_checkpoint: bool = False
    fail_message: str | None = None
    time_after_acceptance: dict[str, float] = field(default_factory=dict)

    def provider(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/kf/sync_msg")
        assert request.extensions["timeout"]["read"] <= get_settings().WECOM_KF_CALLBACK_BUDGET_SECONDS
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json=self.pages.pop(0))

    async def checkpoint(self, key: wecom.WeComKFCursorKey, old: str | None, new: str) -> bool:
        assert key == wecom.WeComKFCursorKey(self.config.id, self.config.app_id or "", "kf")
        self.checkpoints.append((old, new))
        if self.lose_checkpoint:
            self.cursor = "peer"
        if old != self.cursor:
            return False
        self.cursor = new
        return True

    async def accept(self, event: submissions.ChannelEvent) -> submissions.AcceptedChannelEvent:
        assert math.isinf(anyio.current_effective_deadline()), (
            "durable acceptance must run outside the callback deadline"
        )
        _, _, key = json.loads(event.event_key)
        if self.fail_message == key:
            raise RuntimeError("acceptance unavailable")
        self.accepted.append(key)
        self.now = self.time_after_acceptance.get(key, self.now)
        return submissions.AcceptedChannelEvent(replayed=False)

    async def callback(self) -> httpx.Response:
        xml = "<xml><MsgType>event</MsgType><Event>kf_msg_or_event</Event><Token>opaque</Token><OpenKfId>kf</OpenKfId></xml>"
        assert self.config.encrypt_key is not None
        assert self.config.app_id is not None
        cipher = wecom._encrypt_msg(self.config.encrypt_key, xml, self.config.app_id)
        signature = wecom._verify_signature(self.config.verification_token or "", "1", "2", cipher)
        return await self.client.post(
            f"/api/channel/wecom/{self.config.agent_id}/webhook",
            params={"msg_signature": signature, "timestamp": "1", "nonce": "2"},
            content=f"<xml><Encrypt>{cipher}</Encrypt></xml>",
        )


@pytest.fixture
async def pull(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Pull]:
    config = channel_config("wecom")
    config.encrypt_key = base64.b64encode(b"k" * 32).decode().rstrip("=")
    async with webhook_client(wecom.router) as client:
        case = Pull(client=client, config=config)
        original = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(case.provider), **kw)
        )
        monkeypatch.setattr(wecom, "monotonic", lambda: case.now)
        monkeypatch.setattr(wecom.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
        monkeypatch.setattr(wecom, "get_wecom_access_token", AsyncMock(return_value={"access_token": "provider"}))

        async def load(key: wecom.WeComKFCursorKey) -> str | None:
            assert key == wecom.WeComKFCursorKey(config.id, config.app_id or "", "kf")
            return case.cursor

        monkeypatch.setattr(wecom.wecom_kf_cursor_dao, "read", load)
        monkeypatch.setattr(wecom.wecom_kf_cursor_dao, "compare_and_set", case.checkpoint)
        monkeypatch.setattr(submissions, "accept_channel_event", case.accept)
        yield case


def text(key: str) -> JsonObject:
    return {"origin": 3, "msgtype": "text", "msgid": key, "text": {"content": "message"}, "open_kfid": "kf"}


def page(cursor: str, *items: JsonObject, more: bool = False) -> JsonObject:
    return {"errcode": 0, "msg_list": list(items), "next_cursor": cursor, "has_more": int(more)}


async def test_budget_checkpoints_only_settled_pages_and_next_callback_resumes(pull: Pull) -> None:
    pull.pages = [page("one", text("a"), more=True), page("two", text("b"), text("c"))]
    pull.time_after_acceptance["b"] = get_settings().WECOM_KF_CALLBACK_BUDGET_SECONDS - 0.1
    count = budget_stop_count()
    assert (await pull.callback()).status_code == 200
    assert budget_stop_count() == count + 1
    assert pull.accepted == ["a", "b"]
    assert pull.checkpoints == [(None, "one")]
    pull.time_after_acceptance.clear()
    pull.pages = [page("two", text("b"), text("c"))]
    assert (await pull.callback()).status_code == 200
    assert pull.cursor == "two"
    assert pull.accepted[-2:] == ["b", "c"]
    assert pull.sent == [
        {"limit": 20, "open_kfid": "kf", "token": "opaque", "cursor": cursor} for cursor in ("", "one", "one")
    ]


async def test_callback_budget_includes_pre_drain_config_load(pull: Pull, monkeypatch: pytest.MonkeyPatch) -> None:
    async def load(**_: object) -> ChannelConfigRecord:
        pull.now += get_settings().WECOM_KF_CALLBACK_BUDGET_SECONDS
        return pull.config

    monkeypatch.setattr(wecom.channel_config_dao, "get_for_agent", load)
    count = budget_stop_count()
    assert (await pull.callback()).status_code == 200
    assert budget_stop_count() == count + 1
    assert pull.sent == pull.checkpoints == []


async def test_peer_winning_the_checkpoint_is_acknowledged_without_overwriting_it(pull: Pull) -> None:
    pull.cursor = "before"
    pull.lose_checkpoint = True
    pull.pages = [page("after", text("a"))]
    assert (await pull.callback()).status_code == 200
    assert pull.checkpoints == [("before", "after")]
    assert pull.cursor == "peer"


async def test_held_media_on_later_page_leaves_checkpoint_and_does_not_block_other_messages(
    pull: Pull, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(submissions, "_take_claim", AsyncMock(return_value=False))
    download = AsyncMock(side_effect=AssertionError("a held message must not be downloaded"))
    monkeypatch.setattr(wecom, "accept_wecom_media", download)
    media: JsonObject = {**text("held"), "msgtype": "image", "image": {"media_id": "media"}}
    pull.pages = [page("one", text("a"), more=True), page("two", media, text("b"))]
    assert (await pull.callback()).status_code == 200
    assert pull.accepted == ["a", "b"]
    assert pull.checkpoints == []
    download.assert_not_awaited()


async def test_acceptance_failure_does_not_checkpoint_or_attempt_younger_rows(pull: Pull) -> None:
    pull.pages = [page("one", text("a"), more=True), page("two", text("broken"), text("b"))]
    pull.fail_message = "broken"
    assert (await pull.callback()).status_code == 503
    assert pull.accepted == ["a"]
    assert pull.checkpoints == []

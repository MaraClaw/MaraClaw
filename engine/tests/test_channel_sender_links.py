from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import channel_links
from app.records.user import UserRecord
from app.schemas.channel_links import ChannelLinkCodeResponse, ChannelLinkStatus
from app.services.channels import sender_links


@pytest.mark.parametrize("provider", ["telegram", "zalo"])
@pytest.mark.parametrize(
    ("text", "code"), [("/start", None), ("/link", None), (" /link abc-def ", "abc-def"), ("/start ABC", "ABC")]
)
def test_link_command_parser(provider, text, code):
    parsed = sender_links.parse_link_command(provider, text)
    assert parsed is not None
    assert parsed.code == code


@pytest.mark.parametrize("text", ["hello /link ABC", "/linking ABC", "link ABC", "/start@other_bot ABC"])
def test_unrelated_text_is_not_a_link_command(text):
    assert sender_links.parse_link_command("telegram", text) is None


@pytest.mark.parametrize("code", ["", "123", "U" * 10, "0" * 65])
def test_malformed_codes_are_not_normalized(code):
    assert sender_links.normalize_code(code) is None


async def test_member_routes_use_closed_channels_private_responses_and_require_auth(monkeypatch):
    user = UserRecord(id=uuid4())
    app = FastAPI()
    app.include_router(channel_links.router, prefix="/api")
    path = f"/api/agents/{uuid4()}/channel-links/telegram"
    monkeypatch.setattr(
        sender_links,
        "link_status",
        AsyncMock(
            return_value=ChannelLinkStatus(
                channel="telegram", available=True, linked=False, linked_at=None, external_hint=None
            )
        ),
    )
    monkeypatch.setattr(
        sender_links,
        "issue_link_code",
        AsyncMock(
            return_value=ChannelLinkCodeResponse(
                channel="telegram", code="0123456789", expires_at=datetime.now(UTC), deep_link=None, account_hint=None
            )
        ),
    )
    monkeypatch.setattr(sender_links, "unlink", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path + "/code")).status_code == 401
        app.dependency_overrides[channel_links.get_current_user] = lambda: user
        for method, suffix in [("GET", ""), ("POST", "/code"), ("DELETE", "")]:
            response = await client.request(method, path + suffix)
            assert response.status_code in {200, 204}
            assert response.headers["cache-control"] == "no-store, private"
        assert (await client.get(path.replace("telegram", "slack"))).status_code == 422
    app.dependency_overrides.clear()

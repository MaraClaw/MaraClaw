from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest

from app.services.channels.outbound_errors import ProviderDeliveryError, ProviderPayloadRejectedError, retry_at
from app.services.channels.outbound_identity import preflight_outbound
from app.services.channels.outbound_plan import build_plan
from test_channel_delivery_transport import configured, transport


@pytest.mark.parametrize("provider", ["slack", "discord"])
async def test_rotated_tokens_prove_same_public_principal_without_persisting_hash(monkeypatch, provider):
    outbound = configured(provider)
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        if provider == "slack":
            assert request.method == "POST"
            assert request.url == "https://slack.com/api/auth.test"
            return httpx.Response(200, json={"ok": True, "team_id": "T1", "bot_id": "B1"})
        assert request.method == "GET"
        assert request.url == "https://discord.com/api/v10/users/@me"
        return httpx.Response(200, json={"id": "bot-1", "bot": True})

    transport(monkeypatch, respond)
    first = await preflight_outbound(outbound)
    outbound.config.app_secret = "rotated-secret"

    rotated = await preflight_outbound(outbound)

    assert rotated.target == first.target
    assert rotated.target.account == (("account", "T1", "B1") if provider == "slack" else ("account", "bot-1"))
    assert [request.headers["Authorization"] for request in requests] == [
        f"{'Bearer' if provider == 'slack' else 'Bot'} live-secret",
        f"{'Bearer' if provider == 'slack' else 'Bot'} rotated-secret",
    ]
    encoded = build_plan(first.target, "text", outbound.config.id).model_dump_json()
    for credential in ("live-secret", "rotated-secret"):
        assert credential not in encoded
        assert sha256(credential.encode()).hexdigest() not in encoded


@pytest.mark.parametrize("change", ["slack-team", "slack-bot", "discord-bot", "discord-declared-app"])
async def test_public_principal_and_declared_anchor_changes_do_not_match(monkeypatch, change):
    outbound = configured("slack" if change.startswith("slack") else "discord")
    principal = {"ok": True, "team_id": "T1", "bot_id": "B1", "id": "bot-1", "bot": True}
    transport(monkeypatch, lambda _request: httpx.Response(200, json=principal))
    first = await preflight_outbound(outbound)
    match change:
        case "slack-team":
            principal["team_id"] = "T2"
        case "slack-bot":
            principal["bot_id"] = "B2"
        case "discord-bot":
            principal["id"] = "bot-2"
        case "discord-declared-app":
            outbound = type(outbound)(outbound.target.model_copy(update={"account": ("other-app",)}), outbound.config)

    changed = await preflight_outbound(outbound)

    assert changed.target != first.target


@pytest.mark.parametrize("provider", ["slack", "discord"])
async def test_preflight_copies_credentials_and_nested_config_before_http(monkeypatch, provider):
    outbound = configured(provider)

    def respond(request):
        outbound.config.app_secret = "unproved-token"
        outbound.config.extra_config["service_account_json"]["private_key"] = "changed-key"
        assert request.headers["Authorization"].endswith(" live-secret")
        return httpx.Response(200, json={"ok": True, "team_id": "T1", "bot_id": "B1", "id": "bot-1", "bot": True})

    transport(monkeypatch, respond)

    proved = await preflight_outbound(outbound)

    assert proved.snapshot.config.app_secret == "live-secret"
    assert proved.snapshot.config.extra_config["service_account_json"]["private_key"] == "private-key"
    assert proved.snapshot != outbound


@pytest.mark.parametrize(
    ("provider", "status", "body"),
    [
        ("slack", 200, {"ok": False, "error": "msg_too_long"}),
        ("slack", 200, {"ok": True, "team_id": "T1"}),
        ("discord", 200, {"id": "user-1", "bot": False}),
        ("discord", 400, {"code": 50035, "errors": {"content": {"_errors": []}}}),
        ("discord", 401, {"message": "Unauthorized"}),
    ],
)
async def test_identity_failures_are_repairable_not_payload_rejections(monkeypatch, provider, status, body):
    transport(monkeypatch, lambda _request: httpx.Response(status, json=body))

    with pytest.raises((ProviderDeliveryError, httpx.HTTPStatusError)) as error:
        await preflight_outbound(configured(provider))

    assert not isinstance(error.value, ProviderPayloadRejectedError)


@pytest.mark.parametrize("provider", ["slack", "discord"])
async def test_identity_rate_limit_preserves_provider_retry_deadline(monkeypatch, provider):
    now = datetime(2026, 10, 21, tzinfo=UTC)
    transport(monkeypatch, lambda _request: httpx.Response(429, headers={"Retry-After": "900"}, json={}))

    with pytest.raises(httpx.HTTPStatusError) as error:
        await preflight_outbound(configured(provider))

    assert retry_at(error.value, now) == now + timedelta(seconds=900)

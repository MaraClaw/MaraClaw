"""Refresh-token omission preserves credentials while updating access and account."""

from unittest.mock import AsyncMock

import pytest
from tests.test_chatgpt_subscription import _REFRESHED, FakeChatGPTTransport, _model

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.services import chatgpt_oauth, chatgpt_subscription as handoff


async def test_refresh_preserves_refresh_token_when_response_omits_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an encrypted refresh token and a response without a replacement.
    key = get_settings().SECRET_KEY
    refresh = "oa-refresh-retained"
    existing = _model(
        refresh_token_encrypted=encrypt_data(refresh, key),
        oauth_account_id="acct-old",
    )
    transport = FakeChatGPTTransport()
    del transport.token_body["refresh_token"]
    monkeypatch.setattr(chatgpt_oauth, "_transport", transport)
    monkeypatch.setattr(handoff.llm_model_dao, "get", AsyncMock(return_value=existing))
    persist = AsyncMock(return_value=existing)
    monkeypatch.setattr(handoff.llm_model_dao, "update", persist)
    monkeypatch.setattr(handoff, "activate_pool_model_for_tenant", AsyncMock())

    # When the subscription refreshes through the real token parser and encryption.
    await handoff.refresh_subscription_model(existing)

    # Then the old refresh token is persisted alongside new access and account data.
    persist.assert_awaited_once()
    assert persist.await_args is not None
    updates = persist.await_args.kwargs["obj_in"]
    assert decrypt_data(updates["refresh_token_encrypted"], key) == refresh
    assert updates["refresh_token_encrypted"] != refresh
    assert decrypt_data(updates["api_key_encrypted"], key) == _REFRESHED
    assert updates["api_key_encrypted"] != _REFRESHED
    assert updates["oauth_account_id"] == "acct-live"

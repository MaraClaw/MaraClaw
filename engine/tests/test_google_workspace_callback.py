from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse

from app.api import google_workspace
from app.services.auth_provider import AuthProviderPayload, ExternalUserInfo


class InvalidTokenProvider:
    def __init__(self, token_data: AuthProviderPayload) -> None:
        self.token_data = token_data
        self.user_info_calls = 0

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        assert code == "authorization-code"
        assert redirect_uri is None
        return self.token_data

    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        self.user_info_calls += 1
        return ExternalUserInfo(provider_type="google_workspace")


class ErrorLogRecorder:
    def __init__(self) -> None:
        self.error_messages: list[str] = []

    def error(self, message: str) -> None:
        self.error_messages.append(message)


@pytest.mark.asyncio
async def test_google_sso_callback_redacts_invalid_token_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an OAuth exchange with an invalid access token and sensitive response fields.
    secret_sentinels = (
        "refresh-token-secret-sentinel",
        "id-token-secret-sentinel",
        "provider-error-sentinel",
    )
    token_data: AuthProviderPayload = {
        "access_token": "",
        "refresh_token": secret_sentinels[0],
        "id_token": secret_sentinels[1],
        "error": secret_sentinels[2],
    }
    provider = InvalidTokenProvider(token_data)
    log_recorder = ErrorLogRecorder()

    async def get_provider(provider_type: str, tenant_id: str | None) -> InvalidTokenProvider:
        assert provider_type == "google_workspace"
        assert tenant_id is None
        return provider

    async def get_preferred_provider(provider_type: str, tenant_id: str | None = None, **_kwargs) -> None:
        assert provider_type == "google_workspace"
        assert tenant_id is None

    async def get_google_redirect_uri(_db, _provider, _request=None) -> str:
        return "https://example.test/callback"

    monkeypatch.setattr(google_workspace.auth_provider_registry, "get_provider", get_provider)
    monkeypatch.setattr(google_workspace, "get_preferred_identity_provider", get_preferred_provider)
    monkeypatch.setattr(google_workspace, "get_google_redirect_uri", get_google_redirect_uri)
    monkeypatch.setattr(google_workspace, "logger", log_recorder)

    db = MagicMock()
    # When: the SSO callback handles the OAuth exchange response.
    response = await google_workspace._handle_google_sso_callback("authorization-code", None, None, None, db)

    # Then: the public failure response is unchanged and the payload stays out of logs.
    assert isinstance(response, HTMLResponse)
    assert response.status_code == 200
    assert response.body == b"Auth failed: Token exchange error"
    assert provider.user_info_calls == 0
    assert len(log_recorder.error_messages) == 1
    assert all(sentinel not in message for message in log_recorder.error_messages for sentinel in secret_sentinels)
    assert "access token" in log_recorder.error_messages[0].lower()

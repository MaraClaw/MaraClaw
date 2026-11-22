from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.chatgpt_oauth import interpret_token_response
from app.services.chatgpt_pkce import BrowserCallback, parse_callback_query, pkce_challenge


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "ftp://oauth.example.test/callback",
        "https://user:pass@oauth.example.test/callback",
        "https://oauth.example.test/callback?state=s",
        "https://oauth.example.test/callback?",
        "https://oauth.example.test/callback#fragment",
        "https://oauth.example.test/callback#",
        "https://oauth.example.test",
        "https://oauth.example.test/",
        "https:///callback",
        "https://oauth.example.test:not-a-port/callback",
        " https://oauth.example.test/callback",
        "https://oauth.example.test/%ZZ",
        "https://oauth.example.test/{tenant}/callback",
        "https://oauth.example.test/%7Btenant%7D/callback",
        "https://oauth.example.test/call%0Aback",
    ],
)
def test_settings_reject_invalid_chatgpt_redirect(redirect_uri: str) -> None:
    # Given / When
    with pytest.raises(ValidationError) as exc:
        Settings(CHATGPT_OAUTH_REDIRECT_URI=redirect_uri)
    # Then
    assert [(error["loc"], error["type"]) for error in exc.value.errors(include_input=False)] == [
        (("CHATGPT_OAUTH_REDIRECT_URI",), "value_error")
    ]


@pytest.mark.parametrize("client_id", ["", " ", "app client", "app_client\n", "app\x00client"])
def test_settings_reject_blank_chatgpt_client_id(client_id: str) -> None:
    # Given / When
    with pytest.raises(ValidationError) as exc:
        Settings(CHATGPT_OAUTH_CLIENT_ID=client_id)
    # Then
    assert [(error["loc"], error["type"]) for error in exc.value.errors(include_input=False)] == [
        (("CHATGPT_OAUTH_CLIENT_ID",), "value_error")
    ]


def test_settings_default_disables_sign_in_and_accept_custom_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    monkeypatch.delenv("CHATGPT_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("CHATGPT_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    custom_uri = "https://oauth.example.test:8443/team/%63allback"
    # When
    default = Settings()
    custom = Settings(CHATGPT_OAUTH_REDIRECT_URI=custom_uri, CHATGPT_OAUTH_CLIENT_ID="app_custom-1.2")
    explicit_off = Settings(CHATGPT_OAUTH_REDIRECT_URI="")
    configured = [
        (settings.CHATGPT_OAUTH_REDIRECT_URI, settings.CHATGPT_OAUTH_CLIENT_ID)
        for settings in (default, custom, explicit_off)
    ]
    # Then
    assert configured == [
        ("", "app_EMoamEEZ73f0CkXaXp7hrann"),
        (custom_uri, "app_custom-1.2"),
        ("", "app_EMoamEEZ73f0CkXaXp7hrann"),
    ]


def test_pkce_matches_rfc7636_vector():
    # Given / When
    challenge = pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
    # Then
    assert challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@pytest.mark.parametrize(
    "query",
    [
        "",
        "state=s",
        "code=c",
        "error=access_denied",
        "state=&code=c",
        "state=s%21&code=c",
        "state=" + "s" * 257 + "&code=c",
        "state=s&state=s&code=c",
        "state=s&%73tate=s&code=c",
        "state=s&code=c&code=c",
        "state=s&code=c&error=denied",
        "state=s&error=denied&error=denied",
        "state=s&code=c&iss=a&iss=b",
        "state=s&code=",
        "state=s&error=",
        "state=s&code=%20",
        "state=s&code=%ZZ",
        "state=s&code=%FF",
        "state=s&code=%00",
        "state=s&code=a+b",
        "state=s&code=c&broken",
        "state=s&code=c&",
        "state=s&code=c&=v",
        " state=s&code=c",
        "state=s&code=c\n",
        "state=s&code=\\c",
        "state=s&code=c#",
        "state=s&code=c#secret",
        "state=s&code=\u00e9",
        "state=s&code=c&iss=" + "x" * 8192,
        "state=s&code=c&" + "&".join(f"k{index}=v" for index in range(31)),
    ],
)
def test_callback_rejects_malformed_or_ambiguous_query(query: str) -> None:
    # Given / When
    parsed = parse_callback_query(query)
    # Then
    assert parsed is None


def test_callback_accepts_code_or_provider_denial_without_returning_extra_parameters() -> None:
    # Given
    granted = "state=Ab_9-z&code=ac_123.x-y&scope=openid&iss=https%3A%2F%2Fauth.openai.com"
    denied = "state=Ab_9-z&error=access_denied&error_description=User+did+not+approve"
    # When
    code, denial = parse_callback_query(granted), parse_callback_query(denied)
    # Then
    assert code == BrowserCallback(state="Ab_9-z", code="ac_123.x-y", error=None)
    assert denial == BrowserCallback(state="Ab_9-z", code=None, error="access_denied")


@pytest.mark.parametrize("status", [200, 400, 401, 403, 500])
def test_token_failure_never_echoes_provider_prose(status):
    # Given / When
    result = interpret_token_response(status, {"error": "invalid_grant", "error_description": "secret-provider-prose"})
    # Then
    assert result.status != "authorized"
    assert "secret-provider-prose" not in str(result)

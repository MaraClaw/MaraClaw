import pytest
from fastapi import HTTPException

from app.services.chatgpt_oauth import interpret_token_response
from app.services.chatgpt_pkce import parse_browser_callback, pkce_challenge


def test_pkce_matches_rfc7636_vector():
    # Given / When
    challenge = pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
    # Then
    assert challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:1455/auth/callback?state=s&code=c",
        "http://127.0.0.1:1455/auth/callback?state=s&code=c",
        "http://localhost:1456/auth/callback?state=s&code=c",
        "http://localhost/auth/callback?state=s&code=c",
        "http://localhost:1455/auth/callback/?state=s&code=c",
        "http://localhost:1455/auth/%63allback?state=s&code=c",
        "http://user@localhost:1455/auth/callback?state=s&code=c",
        "http://localhost:1455.evil/auth/callback?state=s&code=c",
        "http://localhost:1455/auth/callback?state=s&code=c#",
        "http://localhost:1455/auth/callback?state=s&code=c#secret",
        "http://localhost:1455/auth/callback?state=s&state=s&code=c",
        "http://localhost:1455/auth/callback?state=s&%73tate=s&code=c",
        "http://localhost:1455/auth/callback?state=s&code=c&code=c",
        "http://localhost:1455/auth/callback?state=s&code=c&error=denied",
        "http://localhost:1455/auth/callback?state=s&error=denied&error=denied",
        "http://localhost:1455/auth/callback?state=s",
        "http://localhost:1455/auth/callback?code=c",
        "http://localhost:1455/auth/callback?state=wrong&code=c",
        "http://localhost:1455/auth/callback?state=s&code=",
        "http://localhost:1455/auth/callback?state=s&error=",
        "http://localhost:1455/auth/callback?state=s&code=%ZZ",
        "http://localhost:1455/auth/callback?state=s&code=%FF",
        "http://localhost:1455/auth/callback?state=s&code=%00",
        "http://localhost:1455/auth/callback?state=s&code=a+b",
        "http://localhost:1455/auth/callback?state=s&code=c&broken",
        " http://localhost:1455/auth/callback?state=s&code=c",
        "http://localhost:1455/auth/callback?state=s&code=c\n",
        "http://localhost:1455/auth/callback?state=s&code=\\c",
    ],
)
def test_callback_rejects_malformed_or_ambiguous_url(url):
    # Given / When
    with pytest.raises(HTTPException) as exc:
        parse_browser_callback(url, "s")
    # Then
    assert exc.value.status_code == 400


def test_callback_accepts_normal_provider_denial_description_without_returning_it():
    # Given / When
    parsed = parse_browser_callback(
        "http://localhost:1455/auth/callback?state=s&error=access_denied&error_description=User+did+not+approve", "s"
    )
    # Then
    assert parsed.code is None
    assert parsed.error == "access_denied"


@pytest.mark.parametrize("status", [200, 400, 401, 403, 500])
def test_token_failure_never_echoes_provider_prose(status):
    # Given / When
    result = interpret_token_response(status, {"error": "invalid_grant", "error_description": "secret-provider-prose"})
    # Then
    assert result.status != "authorized"
    assert "secret-provider-prose" not in str(result)

"""JWT wire behavior and the logging sink callable contract."""

import json
from calendar import timegm
from datetime import UTC, datetime, timedelta, tzinfo
from io import StringIO
from typing import Self, override
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from jose import jwt
from pydantic import TypeAdapter, ValidationError

from app.core import security
from app.core.internal_contracts import AccessTokenClaims, AccessTokenPayload, Sink
from app.core.json_types import JsonObject
from app.core.logging.levels import INFO
from app.core.logging.service import LoggingService


def test_claims_adapter_keeps_datetime_and_extra_claims() -> None:
    expiration = datetime(2035, 1, 2, 3, 4, 5, tzinfo=UTC)
    claims = {"sub": "user", "role": "member", "exp": expiration, "gen": 7, "tenant": {"id": "tenant"}}

    parsed = TypeAdapter(AccessTokenClaims).validate_python(claims, strict=True)

    assert parsed["exp"] is expiration
    assert parsed["tenant"] == {"id": "tenant"}


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": 7, "role": "member", "exp": 2000000000},
        {"sub": "user", "role": 7, "exp": 2000000000},
        {"sub": "user", "role": "member", "exp": "2000000000"},
        {"sub": "user", "role": "member"},
    ],
)
def test_payload_adapter_rejects_malformed_required_claims(claims: object) -> None:
    adapter = TypeAdapter(AccessTokenPayload)

    with pytest.raises(ValidationError):
        adapter.validate_python(claims, strict=True)


async def test_access_token_encodes_datetime_as_numeric_date(monkeypatch: pytest.MonkeyPatch) -> None:
    issued_at = datetime(2035, 1, 2, 3, 4, 5, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        @override
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls(2035, 1, 2, 3, 4, 5, tzinfo=UTC)

    monkeypatch.setattr(security, "datetime", Clock)
    identity_id = uuid4()
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))

    token = await security.create_access_token("user", "member", timedelta(seconds=90), identity_id=identity_id)

    payload = security.decode_access_token(token)
    assert payload == {
        "sub": "user",
        "role": "member",
        "exp": timegm((issued_at + timedelta(seconds=90)).utctimetuple()),
        "gen": 7,
    }


def test_decode_accepts_extra_claims_without_exposing_them() -> None:
    token = str(
        jwt.encode(
            {
                "sub": "user",
                "role": "member",
                "exp": datetime.now(UTC) + timedelta(minutes=5),
                "gen": 7,
                "tenant": {"id": "tenant"},
            },
            security.settings.JWT_SECRET_KEY,
            algorithm=security.settings.JWT_ALGORITHM,
        )
    )

    payload = security.decode_access_token(token)

    assert set(payload) == {"sub", "role", "exp", "gen"}
    assert payload["sub"] == "user"
    assert payload["role"] == "member"
    assert TypeAdapter(AccessTokenPayload).validate_python(payload, strict=True) == payload


@pytest.mark.parametrize("generation", [None, True, False, -1, "0", 0.0, [], {}])
def test_decode_rejects_missing_or_malformed_generation(generation: object) -> None:
    # Given a correctly signed token with a missing or invalid generation.
    claims = {"sub": "user", "role": "member", "exp": datetime.now(UTC) + timedelta(minutes=5)}
    if generation is not None:
        claims["gen"] = generation
    token = jwt.encode(claims, security.settings.JWT_SECRET_KEY, algorithm=security.settings.JWT_ALGORITHM)

    # When decoding it, then it is rejected rather than grandfathered in.
    with pytest.raises(HTTPException) as caught:
        security.decode_access_token(token)
    assert caught.value.status_code == 401


@pytest.mark.parametrize("token", ["not-a-jwt", ""])
def test_decode_still_rejects_bad_tokens(token: str) -> None:
    with pytest.raises(HTTPException) as caught:
        security.decode_access_token(token)

    assert caught.value.status_code == 401


def test_logging_accepts_sink_returning_character_count() -> None:
    with StringIO() as stream:
        sink: Sink = stream.write
        service = LoggingService(fmt="json", enqueue=False, sink=sink)

        service.emit(INFO, "character-count", (), {}, name="contract", depth=1, exc_info=None, extra=None)

        record = TypeAdapter(JsonObject).validate_python(json.loads(stream.getvalue()))
        assert record["msg"] == "character-count"


def test_logging_accepts_sink_returning_none() -> None:
    chunks: list[str] = []
    sink: Sink = chunks.append
    service = LoggingService(fmt="json", enqueue=False, sink=sink)

    service.emit(INFO, "no-return", (), {}, name="contract", depth=1, exc_info=None, extra=None)

    assert len(chunks) == 1
    record = TypeAdapter(JsonObject).validate_python(json.loads(chunks[0]))
    assert record["msg"] == "no-return"

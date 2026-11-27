"""Google Chat app helpers: request verification, event parse, outbound send.

Channel transport is an HTTP endpoint Chat app (webhook).  Config mapping on
``channel_configs``:

- ``app_id``: Google Cloud project number (JWT audience for request verification)
- ``app_secret``: optional service-account client email (display only; not required)
- ``encrypt_key``: unused for SA PEMs (too small); kept for legacy compatibility
- ``extra_config.service_account_json``: full SA JSON for proactive / async send
- ``extra_config.audience``: override JWT audience if different from project number
- ``verification_token``: optional legacy shared secret checked against body token

Inbound events are authenticated with Google-signed bearer tokens from
``chat@system.gserviceaccount.com`` when ``app_id`` / audience is set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from jose import JWTError, jwt

from app.core.json_types import (
    JsonObject,
    json_as_int,
    json_as_str,
    json_loads_object,
    json_object_from,
    json_object_from_response,
    json_value_from_response,
    mapping_from_row,
)
from app.core.logging import logger
from app.core.outbound_transport import public_http_client
from app.records.channel_config import ChannelConfigRecord
from app.services import im_token_cache
from app.services.channels.google_chat_payloads import (
    GoogleChatAnnotationPayload,
    GoogleChatEventPayload,
    GoogleChatMessagePayload,
    GoogleChatSpacePayload,
    GoogleChatThreadPayload,
    GoogleChatUserPayload,
    is_chat_payload,
)
from app.services.im_sdk_types import is_object_dict
from app.services.service_contracts import GoogleChatMessage

CHAT_CERTS_URL = "https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com"
CHAT_API_BASE = "https://chat.googleapis.com/v1"
CHAT_ISSUER = "chat@system.gserviceaccount.com"
OAUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
ALLOWED_TOKEN_URIS = frozenset({OAUTH_ENDPOINT})
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
# Practical text budget for a single Chat message (bytes-ish; we use char chunks).
CHAT_MSG_CHUNK = 3500
# Google issues service-account access tokens for an hour, the same bound the assertion asks for.
_TOKEN_LIFETIME_SECONDS = 3600
_certs: dict[str, str] = {}
_certs_expires_at: float = 0.0


@dataclass(frozen=True, slots=True)
class GoogleChatInbound:
    """Normalized inbound Chat app event."""

    event_type: str
    text: str
    space_name: str
    space_type: str
    space_display_name: str
    thread_name: str | None
    sender_name: str
    sender_display_name: str
    sender_email: str
    sender_type: str
    message_name: str | None
    has_attachment: bool
    raw: JsonObject


async def _fetch_chat_certs() -> dict[str, str]:
    global _certs, _certs_expires_at
    now = time.time()
    if _certs and _certs_expires_at > now:
        return _certs
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(CHAT_CERTS_URL)
        _ = resp.raise_for_status()
        certs_raw = json_value_from_response(resp)
    if not is_object_dict(certs_raw):
        raise TypeError("Unexpected Chat certs payload")
    certs_map = {key: value for key, value in json_object_from(certs_raw).items() if isinstance(value, str)}
    _certs = certs_map
    _certs_expires_at = now + 3600
    return certs_map


def _decode_with_key(token: str, key: str, audience: str) -> dict[str, object]:
    claims_raw: object = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=audience,
        options={"verify_at_hash": False},
    )
    claims = mapping_from_row(claims_raw)
    iss = str(claims.get("iss") or "").strip()
    if iss != CHAT_ISSUER:
        raise ValueError(f"Unexpected JWT issuer: {iss!r}")
    return claims


async def verify_google_chat_bearer(authorization: str | None, audience: str) -> dict[str, object]:
    """Verify Authorization: Bearer <jwt> from Google Chat.

    Raises ValueError on failure.
    """
    if authorization in (None, ""):
        raise ValueError("Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise ValueError("Expected Bearer token")
    token = parts[1].strip()
    if not audience.strip():
        raise ValueError("Missing Google Chat audience (project number)")

    try:
        certs = await _fetch_chat_certs()
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    try:
        headers_raw: object = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise ValueError(f"Invalid JWT header: {exc}") from exc
    headers = mapping_from_row(headers_raw)
    kid = json_as_str(headers.get("kid"))
    key = certs.get(kid) if kid not in (None, "") else None
    if key not in (None, ""):
        try:
            return _decode_with_key(token, key, audience)
        except Exception as exc:
            raise ValueError(f"JWT verification failed: {exc}") from exc

    last_error: Exception | None = None
    for pem in certs.values():
        try:
            return _decode_with_key(token, pem, audience)
        except Exception as exc:
            last_error = exc
            continue
    raise ValueError(f"JWT verification failed: {last_error}")


def audience_for_config(config: ChannelConfigRecord) -> str:
    extra = mapping_from_row(config.extra_config)
    audience_raw: object = extra.get("audience") or config.app_id or ""
    return str(audience_raw).strip()


def parse_google_chat_event(body: GoogleChatEventPayload | JsonObject) -> GoogleChatInbound | None:
    """Parse a Chat app interaction event into a normalized inbound message."""
    event_type = str(body.get("type") or "").strip().upper()
    if event_type in {"", "CARD_CLICKED"}:
        return None

    message_raw = body.get("message")
    message = (
        GoogleChatMessagePayload(message_raw)
        if is_chat_payload(message_raw, GoogleChatMessagePayload)
        else GoogleChatMessagePayload()
    )
    space_raw = body.get("space")
    space = (
        GoogleChatSpacePayload(space_raw)
        if is_chat_payload(space_raw, GoogleChatSpacePayload)
        else GoogleChatSpacePayload()
    )
    message_space = message.get("space")
    if not space and is_chat_payload(message_space, GoogleChatSpacePayload):
        space = GoogleChatSpacePayload(message_space)
    sender_raw = message.get("sender")
    sender = (
        GoogleChatUserPayload(sender_raw)
        if is_chat_payload(sender_raw, GoogleChatUserPayload)
        else GoogleChatUserPayload()
    )
    user_raw = body.get("user")
    if not sender and is_chat_payload(user_raw, GoogleChatUserPayload):
        sender = GoogleChatUserPayload(user_raw)

    # Prefer argumentText (already stripped of bot @mention by Google).
    text = str(message.get("argumentText") or "").strip()
    if not text:
        text = str(message.get("text") or "").strip()
        # Strip leading @mention tokens when argumentText is absent.
        if text.startswith("@"):
            parts = text.split(maxsplit=1)
            text = parts[1].strip() if len(parts) > 1 else ""
        # Strip annotated user mentions from plain text when possible.
        annotations = message.get("annotations")
        if isinstance(annotations, list) and text:
            for ann_raw in annotations:
                if not is_chat_payload(ann_raw, GoogleChatAnnotationPayload):
                    continue
                ann = GoogleChatAnnotationPayload(ann_raw)
                if str(ann.get("type") or "").upper() != "USER_MENTION":
                    continue
                start: object | None = ann.get("startIndex")
                length: object | None = ann.get("length")
                if isinstance(start, int) and isinstance(length, int) and start == 0:
                    text = text[length:].lstrip()
                    break

    thread_raw = message.get("thread")
    thread = (
        GoogleChatThreadPayload(thread_raw)
        if is_chat_payload(thread_raw, GoogleChatThreadPayload)
        else GoogleChatThreadPayload()
    )
    space_name = str(space.get("name") or "").strip()
    space_type = str(space.get("type") or space.get("spaceType") or "").strip().upper()
    space_display = str(space.get("displayName") or space.get("name") or "").strip()
    sender_name = str(sender.get("name") or "").strip()
    sender_type = str(sender.get("type") or "").strip().upper()
    has_attachment = bool(message.get("attachment") or message.get("attachments"))

    if event_type == "MESSAGE" and sender_type == "BOT":
        return None
    if event_type == "MESSAGE" and not text and not has_attachment:
        return None

    # Membership events may carry an embedded first message.
    if event_type in {"ADDED_TO_SPACE", "REMOVED_FROM_SPACE"} and not message:
        return GoogleChatInbound(
            event_type=event_type,
            text="",
            space_name=space_name,
            space_type=space_type,
            space_display_name=space_display,
            thread_name=None,
            sender_name=sender_name,
            sender_display_name=str(sender.get("displayName") or "").strip(),
            sender_email=str(sender.get("email") or "").strip(),
            sender_type=sender_type,
            message_name=None,
            has_attachment=False,
            raw=body,
        )

    return GoogleChatInbound(
        event_type=event_type,
        text=text,
        space_name=space_name,
        space_type=space_type,
        space_display_name=space_display,
        thread_name=str(thread.get("name") or "").strip() or None,
        sender_name=sender_name,
        sender_display_name=str(sender.get("displayName") or "").strip(),
        sender_email=str(sender.get("email") or "").strip(),
        sender_type=sender_type,
        message_name=str(message.get("name") or "").strip() or None,
        has_attachment=has_attachment,
        raw=body,
    )


def external_conv_id_for_inbound(event: GoogleChatInbound) -> str:
    if event.thread_name not in (None, ""):
        return f"google_chat_{event.thread_name}"
    if event.space_name:
        return f"google_chat_{event.space_name}"
    return f"google_chat_{event.sender_name or 'unknown'}"


def is_group_space(event: GoogleChatInbound) -> bool:
    if event.space_type in {"ROOM", "SPACE", "GROUP_CHAT"}:
        return True
    if event.space_type in {"DM", "DIRECT_MESSAGE", "DIRECT_MESSAGE_SPACE"}:
        return False
    # Unknown type: prefer group when we only have a shared space resource.
    return bool(event.space_name.startswith("spaces/") and event.space_type == "")


def sync_text_response(text: str, *, thread_name: str | None = None) -> dict[str, object]:
    """Build a synchronous Chat app response body."""
    payload: GoogleChatMessage = {"text": text}
    if thread_name not in (None, ""):
        payload["thread"] = {"name": thread_name}
    return dict(payload)


def chunk_text(text: str, *, limit: int = CHAT_MSG_CHUNK) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def parse_external_conv_id(external: str) -> tuple[str, str | None]:
    """Return (space_name, thread_name|None) from a stored external_conv_id."""
    resource = external.removeprefix("google_chat_")
    space_name = resource
    thread_name: str | None = None
    if "/threads/" in resource:
        space_name, thread_part = resource.split("/threads/", 1)
        thread_name = f"{space_name}/threads/{thread_part}"
    if not space_name.startswith("spaces/"):
        raise ValueError(f"Invalid Google Chat space reference: {external}")
    return space_name, thread_name


def _service_account_info(config: ChannelConfigRecord) -> dict[str, object] | None:
    extra = mapping_from_row(config.extra_config)
    raw: object = extra.get("service_account_json")
    if is_object_dict(raw):
        return mapping_from_row(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json_loads_object(raw)
            if parsed:
                return mapping_from_row(parsed)
        except json.JSONDecodeError:
            logger.warning("[GoogleChat] extra_config.service_account_json is not valid JSON")
    # Legacy: never prefer encrypt_key PEM path for new writes; still read if present
    # and short enough that it might be a non-PEM marker (should not happen).
    email = (config.app_secret or "").strip()
    private_key = (config.encrypt_key or "").strip()
    if email and private_key and "BEGIN PRIVATE KEY" in private_key:
        return {
            "type": "service_account",
            "client_email": email,
            "private_key": private_key.replace("\\n", "\n"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    return None


def _service_account_credentials(sa_info: dict[str, object]) -> tuple[str, str]:
    """Client email and PEM private key of a service account whose ``token_uri`` is allowed."""
    client_email = sa_info.get("client_email")
    private_key = sa_info.get("private_key")
    if not client_email or not private_key:
        raise ValueError("Service account missing client_email/private_key")
    # Fail closed: never POST assertions to attacker-controlled token URIs.
    raw_uri = str(sa_info.get("token_uri") or OAUTH_ENDPOINT).strip()
    if raw_uri not in ALLOWED_TOKEN_URIS:
        raise ValueError(f"Disallowed service-account token_uri: {raw_uri}")
    return str(client_email), str(private_key).replace("\\n", "\n")


def _service_account_token_key(client_email: str, private_key: str, scopes: list[str]) -> im_token_cache.ImTokenKey:
    """One cache entry per account, key material and scopes; a rotated key never reuses a token, and only its digest is held."""
    key_digest = hashlib.sha256(private_key.encode()).hexdigest()
    return im_token_cache.ImTokenKey("google_chat", client_email, f"{key_digest}\0{' '.join(scopes)}")


def _sign_assertion(claims: dict[str, object], private_key: str) -> str:
    return str(jwt.encode(claims, private_key, algorithm="RS256"))


async def _mint_service_account_token(
    client_email: str, private_key: str, scopes: list[str], *, pinned: bool
) -> im_token_cache.MintedToken:
    now = int(time.time())
    # RSA signing is CPU-bound; keep it off the event loop.
    assertion = await asyncio.to_thread(
        _sign_assertion,
        {
            "iss": client_email,
            "sub": client_email,
            "aud": OAUTH_ENDPOINT,
            "iat": now,
            "exp": now + _TOKEN_LIFETIME_SECONDS,
            "scope": " ".join(scopes),
        },
        private_key,
    )
    client_context = (
        public_http_client(timeout=15, origin="https://oauth2.googleapis.com")
        if pinned
        else httpx.AsyncClient(timeout=15, follow_redirects=False)
    )
    async with client_context as client:
        resp = await client.post(
            OAUTH_ENDPOINT,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
    if resp.status_code != 200:
        raise im_token_cache.ImTokenRefusedError(resp)
    data = json_object_from_response(resp)
    token = json_as_str(data.get("access_token"))
    if token in (None, ""):
        raise im_token_cache.ImTokenRefusedError(resp)
    return im_token_cache.MintedToken(token, json_as_int(data.get("expires_in")) or _TOKEN_LIFETIME_SECONDS)


async def _service_account_access_token(sa_info: dict[str, object], scopes: list[str], *, pinned: bool = False) -> str:
    """Shared cached service-account access token; a miss mints one via the JWT bearer grant (no google-auth dep).

    Concurrent callers across workers share one mint, and a refused exchange is never cached. ``pinned`` sends the
    exchange through the pinned public client that media downloads use.
    """
    client_email, private_key = _service_account_credentials(sa_info)

    async def mint() -> im_token_cache.MintedToken:
        return await _mint_service_account_token(client_email, private_key, scopes, pinned=pinned)

    try:
        return await im_token_cache.get_or_mint_im_token(
            _service_account_token_key(client_email, private_key, scopes), mint
        )
    except im_token_cache.ImTokenRefusedError as refused:
        status = refused.response.status_code
        if status == 200:
            raise ValueError("Token exchange returned no access_token") from refused
        if pinned:
            raise ValueError(
                f"Google Chat media token exchange failed ({status}); verify service account access"
            ) from refused
        raise ValueError(f"Token exchange failed: {status}") from refused


async def renew_rejected_service_account_token(
    sa_info: dict[str, object], scopes: list[str], rejected: str, *, pinned: bool = False
) -> str:
    """Drop the token Google rejected (only while it is still the cached one) and return a fresh one."""
    client_email, private_key = _service_account_credentials(sa_info)
    await im_token_cache.drop_rejected_im_token(_service_account_token_key(client_email, private_key, scopes), rejected)
    return await _service_account_access_token(sa_info, scopes, pinned=pinned)


async def post_chat_message(
    client: httpx.AsyncClient,
    sa_info: dict[str, object],
    space_name: str,
    *,
    token: str,
    params: dict[str, str],
    payload: Mapping[str, object],
) -> tuple[httpx.Response, str]:
    """POST one Chat message; a 401 renews the token and retries once with the same ``params``.

    ``requestId`` stays in ``params``, so a message Google already accepted is not duplicated. Returns the response
    and the token that produced it.
    """
    url = f"{CHAT_API_BASE}/{space_name}/messages"
    response = await client.post(url, params=params, headers={"Authorization": f"Bearer {token}"}, json=payload)
    if response.status_code == 401:
        token = await renew_rejected_service_account_token(sa_info, [CHAT_BOT_SCOPE], token)
        response = await client.post(url, params=params, headers={"Authorization": f"Bearer {token}"}, json=payload)
    return response, token


async def send_google_chat_message(
    config: ChannelConfigRecord,
    *,
    space_name: str,
    text: str,
    thread_name: str | None = None,
    strict: bool = False,
) -> dict[str, object]:
    """Proactively post a message into a Google Chat space via Chat API."""
    sa_info = _service_account_info(config)
    if sa_info is None or not sa_info:
        raise ValueError(
            "Google Chat proactive send requires extra_config.service_account_json with client_email and private_key"
        )
    if not space_name.startswith("spaces/"):
        raise ValueError(f"Invalid space name: {space_name}")

    access_token = await _service_account_access_token(sa_info, scopes=[CHAT_BOT_SCOPE])
    last: dict[str, object] = {}
    chunks = chunk_text(text)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for chunk in chunks:
            body: GoogleChatMessage = {"text": chunk}
            params: dict[str, str] = {}
            if thread_name not in (None, ""):
                body["thread"] = {"name": thread_name}
                params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
            resp, access_token = await post_chat_message(
                client, sa_info, space_name, token=access_token, params=params, payload=body
            )
            if strict:
                _ = resp.raise_for_status()
            if resp.status_code >= 400:
                raise ValueError(f"Chat API send failed: {resp.status_code} {resp.text[:300]}")
            last = mapping_from_row(json_object_from_response(resp))
            if strict and not bool(json_as_str(last.get("name"))):
                raise RuntimeError("Google Chat did not acknowledge the message")
    return last

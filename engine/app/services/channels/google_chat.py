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

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from jose import JWTError, jwt

from app.core.logging import logger
from app.records.channel_config import ChannelConfigRecord

CHAT_CERTS_URL = (
    "https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com"
)
CHAT_API_BASE = "https://chat.googleapis.com/v1"
CHAT_ISSUER = "chat@system.gserviceaccount.com"
ALLOWED_TOKEN_URIS = frozenset({"https://oauth2.googleapis.com/token"})
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
# Practical text budget for a single Chat message (bytes-ish; we use char chunks).
CHAT_MSG_CHUNK = 3500
_CERT_CACHE: dict[str, Any] = {"expires_at": 0.0, "certs": {}}


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
    raw: dict[str, Any]


async def _fetch_chat_certs() -> dict[str, str]:
    now = time.time()
    if _CERT_CACHE["certs"] and _CERT_CACHE["expires_at"] > now:
        return _CERT_CACHE["certs"]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(CHAT_CERTS_URL)
        resp.raise_for_status()
        certs = resp.json()
    if not isinstance(certs, dict):
        raise ValueError("Unexpected Chat certs payload")
    _CERT_CACHE["certs"] = certs
    _CERT_CACHE["expires_at"] = now + 3600
    return certs


def _decode_with_key(token: str, key: str, audience: str) -> dict[str, Any]:
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=audience,
        options={"verify_at_hash": False},
    )
    iss = str(claims.get("iss") or "").strip()
    if iss != CHAT_ISSUER:
        raise ValueError(f"Unexpected JWT issuer: {iss!r}")
    return claims


async def verify_google_chat_bearer(authorization: str | None, audience: str) -> dict[str, Any]:
    """Verify Authorization: Bearer <jwt> from Google Chat.

    Raises ValueError on failure.
    """
    if not authorization:
        raise ValueError("Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise ValueError("Expected Bearer token")
    token = parts[1].strip()
    if not audience.strip():
        raise ValueError("Missing Google Chat audience (project number)")

    certs = await _fetch_chat_certs()
    try:
        headers = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise ValueError(f"Invalid JWT header: {exc}") from exc
    kid = headers.get("kid")
    key = certs.get(kid) if kid else None
    if key:
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
    extra = config.extra_config or {}
    return str(extra.get("audience") or config.app_id or "").strip()


def has_service_account(config: ChannelConfigRecord) -> bool:
    return _service_account_info(config) is not None


def parse_google_chat_event(body: dict[str, Any]) -> GoogleChatInbound | None:
    """Parse a Chat app interaction event into a normalized inbound message."""
    event_type = str(body.get("type") or "").strip().upper()
    if event_type in {"", "CARD_CLICKED"}:
        return None

    message_raw = body.get("message")
    message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}
    space_raw = body.get("space")
    space: dict[str, Any] = space_raw if isinstance(space_raw, dict) else {}
    message_space = message.get("space")
    if not space and isinstance(message_space, dict):
        space = message_space
    sender_raw = message.get("sender")
    sender: dict[str, Any] = sender_raw if isinstance(sender_raw, dict) else {}
    user_raw = body.get("user")
    if not sender and isinstance(user_raw, dict):
        sender = user_raw

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
            for ann in annotations:
                if not isinstance(ann, dict):
                    continue
                if str(ann.get("type") or "").upper() != "USER_MENTION":
                    continue
                start = ann.get("startIndex")
                length = ann.get("length")
                if isinstance(start, int) and isinstance(length, int) and start == 0:
                    text = text[length:].lstrip()
                    break

    thread_raw = message.get("thread")
    thread: dict[str, Any] = thread_raw if isinstance(thread_raw, dict) else {}
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
    if event.thread_name:
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


def sync_text_response(text: str, *, thread_name: str | None = None) -> dict[str, Any]:
    """Build a synchronous Chat app response body."""
    payload: dict[str, Any] = {"text": text}
    if thread_name:
        payload["thread"] = {"name": thread_name}
    return payload


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


def _service_account_info(config: ChannelConfigRecord) -> dict[str, Any] | None:
    extra = config.extra_config or {}
    raw = extra.get("service_account_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
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


async def _service_account_access_token(sa_info: dict[str, Any], scopes: list[str]) -> str:
    """Mint a service-account access token via JWT bearer grant (no google-auth dep)."""
    now = int(time.time())
    client_email = sa_info.get("client_email")
    private_key = sa_info.get("private_key")
    if not client_email or not private_key:
        raise ValueError("Service account missing client_email/private_key")

    # Fail closed: never POST assertions to attacker-controlled token URIs.
    token_uri = next(iter(ALLOWED_TOKEN_URIS))
    raw_uri = str(sa_info.get("token_uri") or token_uri).strip()
    if raw_uri not in ALLOWED_TOKEN_URIS:
        raise ValueError(f"Disallowed service-account token_uri: {raw_uri}")

    assertion = jwt.encode(
        {
            "iss": client_email,
            "sub": client_email,
            "aud": token_uri,
            "iat": now,
            "exp": now + 3600,
            "scope": " ".join(scopes),
        },
        private_key.replace("\\n", "\n") if isinstance(private_key, str) else private_key,
        algorithm="RS256",
    )
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        resp = await client.post(
            token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        if resp.status_code >= 400:
            raise ValueError(f"Token exchange failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError("Token exchange returned no access_token")
    return str(token)


async def send_google_chat_message(
    config: ChannelConfigRecord,
    *,
    space_name: str,
    text: str,
    thread_name: str | None = None,
) -> dict[str, Any]:
    """Proactively post a message into a Google Chat space via Chat API."""
    sa_info = _service_account_info(config)
    if not sa_info:
        raise ValueError(
            "Google Chat proactive send requires extra_config.service_account_json "
            "with client_email and private_key"
        )
    if not space_name.startswith("spaces/"):
        raise ValueError(f"Invalid space name: {space_name}")

    access_token = await _service_account_access_token(sa_info, scopes=[CHAT_BOT_SCOPE])
    last: dict[str, Any] = {}
    chunks = chunk_text(text)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for chunk in chunks:
            body: dict[str, Any] = {"text": chunk}
            params: dict[str, str] = {}
            if thread_name:
                body["thread"] = {"name": thread_name}
                params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
            url = f"{CHAT_API_BASE}/{space_name}/messages"
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                params=params,
                json=body,
            )
            if resp.status_code >= 400:
                raise ValueError(f"Chat API send failed: {resp.status_code} {resp.text[:300]}")
            last = resp.json()
    return last

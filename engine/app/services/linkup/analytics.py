"""Capture billed Linkup calls as privacy-safe web_search_events."""

from __future__ import annotations

import hmac
import unicodedata
from hashlib import sha256
from json import JSONDecodeError
from urllib.parse import urlparse
from uuid import UUID

from app.config import get_settings
from app.core.json_types import json_as_str, json_loads_object, json_loads_value, json_object_from
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.web_search_event_dao import web_search_event_dao
from app.db.session import optional_connection_ctx
from app.services.linkup.errors import is_quota_error

BILLED_KINDS = frozenset({"search", "fetch", "research", "extract"})
SCHEMA_VERSION = 1
_QUERY_MAX = 500
_DEPTH_MAX = 20
_OUTPUT_TYPE_MAX = 40


def is_billed_call(method: str, path: str) -> bool:
    """True for POST /v1/{search,fetch,research,extract} with no extra path."""
    if method.upper() != "POST":
        return False
    cleaned = path.lstrip("/")
    kind, _, rest = cleaned.partition("/")
    return kind in BILLED_KINDS and rest == ""


def normalize_text(value: str) -> str:
    cleaned = unicodedata.normalize("NFKC", value).lower()
    return " ".join(cleaned.split())[:_QUERY_MAX]


def hash_text(value: str, *, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), sha256).hexdigest()


def host_only(url: str) -> str:
    raw = url.strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").lower()


def status_class_for(status_code: int, body: str) -> str:
    if is_quota_error(status_code, body):
        return "quota"
    if 200 <= status_code < 400:
        return "ok"
    if 400 <= status_code < 500:
        return "client_error"
    return "upstream_error"


def _parse_body(content: bytes | None) -> dict[str, object]:
    if not content:
        return {}
    return dict(json_loads_object(content))


def cheap_result_count(body: str) -> int | None:
    try:
        payload = json_loads_value(body)
    except JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    mapping = json_object_from(payload)
    for key in ("results", "sources", "items"):
        value = mapping.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _hash_secret() -> str:
    settings = get_settings()
    dedicated = settings.WEB_SEARCH_ANALYTICS_HASH_KEY.strip()
    return dedicated or settings.SECRET_KEY


def _raw_and_normalized(kind: str, payload: dict[str, object]) -> tuple[str, str, str]:
    """Return (raw_for_len, normalized, primary_domain)."""
    if kind in {"fetch", "extract"}:
        raw_url = json_as_str(payload.get("url")) or json_as_str(payload.get("link")) or ""
        host = host_only(raw_url)
        return raw_url, host, host
    raw_query = json_as_str(payload.get("query")) or json_as_str(payload.get("q")) or ""
    return raw_query, normalize_text(raw_query), ""


def _clip(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    return value[:limit]


def should_stage_raw_query() -> bool:
    """Raw text is staged only when export can actually land it."""
    settings = get_settings()
    if not settings.WEB_SEARCH_ANALYTICS_INCLUDE_RAW:
        return False
    if not settings.WEB_SEARCH_ANALYTICS_EXPORT_ENABLED:
        return False
    return bool((settings.ANALYTICS_S3_BUCKET or settings.S3_BUCKET).strip())


async def record_linkup_call(
    *,
    agent_id: UUID | None,
    method: str,
    path: str,
    content: bytes | None,
    status: int,
    body: str,
    latency_ms: int,
    key_id: UUID | None,
    quota_rotated: bool = False,
) -> None:
    """Insert one event on a fresh connection. Never raises to the caller."""
    try:
        await _record_linkup_call(
            agent_id=agent_id,
            method=method,
            path=path,
            content=content,
            status=status,
            body=body,
            latency_ms=latency_ms,
            key_id=key_id,
            quota_rotated=quota_rotated,
        )
    except Exception:
        logger.exception("web search analytics capture failed")


async def _record_linkup_call(
    *,
    agent_id: UUID | None,
    method: str,
    path: str,
    content: bytes | None,
    status: int,
    body: str,
    latency_ms: int,
    key_id: UUID | None,
    quota_rotated: bool = False,
) -> None:
    settings = get_settings()
    if not settings.WEB_SEARCH_ANALYTICS_CAPTURE_ENABLED:
        return
    if not is_billed_call(method, path):
        return

    cleaned = path.lstrip("/")
    kind = cleaned.split("/", 1)[0]
    payload = _parse_body(content)
    raw, normalized, domain = _raw_and_normalized(kind, payload)
    secret = _hash_secret()
    hashed = hash_text(normalized, secret=secret)
    export_ready = settings.WEB_SEARCH_ANALYTICS_EXPORT_ENABLED and bool(
        (settings.ANALYTICS_S3_BUCKET or settings.S3_BUCKET).strip()
    )
    export_state = "pending" if export_ready else "skipped"
    include_raw = should_stage_raw_query() and bool(raw)

    tenant_id: UUID | None = None
    if agent_id is not None:
        agent = await agent_dao.get(agent_id)
        if agent is not None:
            tenant_id = agent.tenant_id

    klass = status_class_for(status, body)
    error_class = "quota_rotated" if quota_rotated and klass == "ok" else (None if klass == "ok" else klass)
    depth = json_as_str(payload.get("depth")) or json_as_str(payload.get("reasoningDepth"))
    output_type = json_as_str(payload.get("outputType") or payload.get("output_type"))
    job_id = None
    if kind in {"research", "extract"} and 200 <= status < 400:
        try:
            parsed = json_loads_value(body)
        except JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            job_id = json_as_str(json_object_from(parsed).get("id")) or None

    async with optional_connection_ctx():
        record = await web_search_event_dao.create(
            obj_in={
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "kind": kind,
                "billed": True,
                "method": method.upper(),
                "http_status": status,
                "status_class": klass,
                "latency_ms": max(0, latency_ms),
                "key_id": key_id,
                "query_hash": hashed,
                "query_normalized": normalized,
                "query_char_len": len(raw),
                "depth": _clip(depth, _DEPTH_MAX),
                "output_type": _clip(output_type, _OUTPUT_TYPE_MAX),
                "primary_domain": domain or None,
                "result_count": cheap_result_count(body),
                "error_class": error_class,
                "upstream_job_id": job_id,
                "request_bytes": len(content) if content else 0,
                "response_bytes": len(body.encode("utf-8")),
                "export_state": export_state,
                "schema_version": SCHEMA_VERSION,
            }
        )
        if include_raw:
            await web_search_event_dao.insert_payload(event_id=record.id, raw_query=raw)

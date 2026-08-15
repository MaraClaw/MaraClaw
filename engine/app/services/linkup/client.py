"""Forward Linkup API calls with one-cycle quota rotation."""

from __future__ import annotations

import json
from uuid import UUID

from app.config import get_settings
from app.core.json_types import json_as_str, json_object_from
from app.services.linkup.errors import is_quota_error, is_transient_error
from app.services.linkup.jobs import LinkupJobKeyRemovedError, bind_job, key_for_job
from app.services.linkup.keys import (
    DuplicateLinkupKeyError,
    add_key,
    advance_cursor,
    current_key,
    decrypt_api_key,
    ensure_env_key_seeded,
    mark_exhausted,
    touch_used,
)

UPSTREAM_BASE = "https://api.linkup.so"
ALLOWED_PREFIXES = ("search", "fetch", "research", "extract")


class LinkupProxyError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(body)
        self.status_code = status_code
        self.body = body


def allowed_upstream_path(path: str) -> bool:
    cleaned = path.lstrip("/")
    head = cleaned.split("/", 1)[0]
    return head in ALLOWED_PREFIXES


def _httpx_client(*args: object, **kwargs: object):
    import httpx

    return httpx.AsyncClient(*args, **kwargs)


async def _send(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    content: bytes | None,
    api_key: str,
) -> tuple[int, str, dict[str, str]]:
    outbound = {key: value for key, value in headers.items() if key.lower() not in {"host", "authorization"}}
    outbound["Authorization"] = f"Bearer {api_key}"
    async with _httpx_client(follow_redirects=True, timeout=60.0) as client:
        response = await client.request(method, url, headers=outbound, content=content)
        text = response.text
        out_headers = {key: value for key, value in response.headers.items() if key.lower() != "transfer-encoding"}
        return response.status_code, text, out_headers


def _extract_job_id(body: str) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    mapping = json_object_from(payload)
    job_id = json_as_str(mapping.get("id"))
    return job_id or None


async def _env_fallback_key() -> str | None:
    env_key = get_settings().LINKUP_API_KEY.strip()
    if not env_key:
        return None
    try:
        record = await add_key(label="Environment LINKUP_API_KEY", api_key=env_key)
        return decrypt_api_key(record)
    except DuplicateLinkupKeyError:
        current = await current_key()
        return decrypt_api_key(current) if current is not None else env_key
    except Exception:
        return env_key


async def proxy_linkup(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    content: bytes | None,
) -> tuple[int, str, dict[str, str]]:
    if not allowed_upstream_path(path):
        raise LinkupProxyError(404, "Unknown Linkup path")

    await ensure_env_key_seeded()
    cleaned = path.lstrip("/")
    kind = cleaned.split("/", 1)[0]
    rest = cleaned.split("/", 1)[1] if "/" in cleaned else ""
    url = f"{UPSTREAM_BASE}/v1/{cleaned}"

    if method.upper() == "GET" and kind in {"research", "extract"} and rest:
        try:
            bound = await key_for_job(rest.split("/", 1)[0])
        except LinkupJobKeyRemovedError as exc:
            raise LinkupProxyError(410, str(exc)) from exc
        status, body, out_headers = await _send(
            method=method,
            url=url,
            headers=headers,
            content=content,
            api_key=decrypt_api_key(bound),
        )
        return status, body, out_headers

    start = await current_key()
    if start is None:
        env_key = await _env_fallback_key()
        if env_key is None:
            raise LinkupProxyError(503, "no Linkup API keys configured")
        status, body, out_headers = await _send(
            method=method,
            url=url,
            headers=headers,
            content=content,
            api_key=env_key,
        )
        return status, body, out_headers

    seen: set[UUID] = set()
    record = start
    last: tuple[int, str, dict[str, str]] | None = None
    transient_retries = 0

    while record is not None and record.id not in seen:
        seen.add(record.id)
        await touch_used(record.id)
        status, body, out_headers = await _send(
            method=method,
            url=url,
            headers=headers,
            content=content,
            api_key=decrypt_api_key(record),
        )
        last = (status, body, out_headers)

        if is_quota_error(status, body):
            await mark_exhausted(record.id, message=body[:200])
            record = await advance_cursor(record.id)
            transient_retries = 0
            continue

        if is_transient_error(status) and transient_retries < 1:
            transient_retries += 1
            seen.discard(record.id)
            continue

        if status < 400 and kind in {"research", "extract"} and method.upper() == "POST":
            job_id = _extract_job_id(body)
            if job_id:
                await bind_job(upstream_job_id=job_id, key_id=record.id, kind=kind)
        return status, body, out_headers

    if last is None:
        raise LinkupProxyError(503, "no Linkup API keys configured")
    return last

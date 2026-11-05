"""Forward Linkup API calls with one-cycle quota rotation, bounded by one deadline and a response ceiling."""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from dataclasses import dataclass
from json import JSONDecodeError
from typing import TYPE_CHECKING, final
from uuid import UUID

if TYPE_CHECKING:
    import httpx

from app.config import get_settings
from app.core.json_types import json_as_str, json_loads_value, json_object_from
from app.services.linkup.errors import (
    LinkupDeadlineExceededError,
    LinkupLimitError,
    LinkupResponseTooLargeError,
    is_quota_error,
    is_transient_error,
)
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
_PHASE_TIMEOUT_SECONDS = 60.0


@final
class LinkupProxyError(Exception):
    status_code: int
    body: str

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(body)
        self.status_code = status_code
        self.body = body


def allowed_upstream_path(path: str) -> bool:
    cleaned = path.lstrip("/")
    head = cleaned.split("/", 1)[0]
    return head in ALLOWED_PREFIXES


def _httpx_client(*, follow_redirects: bool, timeout: float) -> httpx.AsyncClient:
    import httpx

    return httpx.AsyncClient(follow_redirects=follow_redirects, timeout=timeout)


def _monotonic() -> float:
    return time.monotonic()


@final
class _Deadline:
    """One monotonic budget for a whole proxied call: rotation, retry, env fallback and bound polls."""

    __slots__ = ("_expires_at", "bytes_read")

    def __init__(self, seconds: float) -> None:
        self._expires_at = _monotonic() + seconds
        self.bytes_read = 0

    def remaining(self) -> float:
        return self._expires_at - _monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise LinkupDeadlineExceededError(bytes_read=self.bytes_read)


@dataclass(slots=True)
class _CallState:
    """Accumulate attribution across attempts for the single analytics write."""

    key_id: UUID | None = None
    quota_rotated: bool = False
    result: tuple[int, str, dict[str, str]] | None = None


async def _send(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    content: bytes | None,
    api_key: str,
    deadline: _Deadline,
    max_response_bytes: int,
) -> tuple[int, str, dict[str, str]]:
    import httpx

    deadline.check()
    outbound = {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"host", "authorization", "content-length", "transfer-encoding"}
    }
    outbound["Authorization"] = f"Bearer {api_key}"
    deadline.bytes_read = 0
    timeout = min(_PHASE_TIMEOUT_SECONDS, deadline.remaining())
    async with _httpx_client(follow_redirects=False, timeout=timeout) as client:
        request = client.build_request(method, url, headers=outbound, content=content)
        redirects = 0
        while True:
            deadline.check()
            if redirects > client.max_redirects:
                raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)
            deadline.bytes_read = 0
            # HTTPX builds the next request (method, auth, cookies), but must not buffer redirect bodies itself.
            async with aclosing(await client.send(request, stream=True, follow_redirects=False)) as response:
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes():
                    deadline.bytes_read += len(chunk)
                    if deadline.bytes_read > max_response_bytes:
                        raise LinkupResponseTooLargeError(bytes_read=deadline.bytes_read)
                    deadline.check()
                    if response.next_request is None:
                        chunks.append(chunk)
                deadline.check()
                if response.next_request is None:
                    text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    out_headers = {
                        key: value for key, value in response.headers.items() if key.lower() != "transfer-encoding"
                    }
                    return response.status_code, text, out_headers
                request = response.next_request
                redirects += 1


def _extract_job_id(body: str) -> str | None:
    try:
        payload = json_loads_value(body)
    except JSONDecodeError:
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


async def _record_result(
    result: tuple[int, str, dict[str, str]],
    *,
    agent_id: UUID | None,
    method: str,
    path: str,
    content: bytes | None,
    key_id: UUID | None,
    started: float,
    quota_rotated: bool = False,
    local_failure: LinkupLimitError | None = None,
) -> tuple[int, str, dict[str, str]]:
    from app.services.linkup.analytics import record_linkup_call

    status, body, _headers = result
    await record_linkup_call(
        agent_id=agent_id,
        method=method,
        path=path,
        content=content,
        status=status,
        body=body,
        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        key_id=key_id,
        quota_rotated=quota_rotated,
        local_failure=local_failure,
    )
    return result


async def _record_failure(
    exc: LinkupProxyError | LinkupLimitError,
    *,
    agent_id: UUID | None,
    method: str,
    path: str,
    content: bytes | None,
    key_id: UUID | None,
    started: float,
    quota_rotated: bool = False,
) -> None:
    _ = await _record_result(
        (exc.status_code, exc.body, {}),
        agent_id=agent_id,
        method=method,
        path=path,
        content=content,
        key_id=key_id,
        started=started,
        quota_rotated=quota_rotated,
        local_failure=exc if isinstance(exc, LinkupLimitError) else None,
    )


async def _send_or_unavailable(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    content: bytes | None,
    api_key: str,
    deadline: _Deadline,
    max_response_bytes: int,
) -> tuple[int, str, dict[str, str]]:
    try:
        return await _send(
            method=method,
            url=url,
            headers=headers,
            content=content,
            api_key=api_key,
            deadline=deadline,
            max_response_bytes=max_response_bytes,
        )
    except LinkupLimitError:
        raise
    except Exception as exc:
        # A transport error that lands after the budget is gone is the deadline's doing, not the upstream's.
        if deadline.expired():
            raise LinkupDeadlineExceededError(bytes_read=deadline.bytes_read) from exc
        raise LinkupProxyError(502, "Linkup upstream unavailable") from exc


async def proxy_linkup(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    content: bytes | None,
    agent_id: UUID | None = None,
) -> tuple[int, str, dict[str, str]]:
    if not allowed_upstream_path(path):
        raise LinkupProxyError(404, "Unknown Linkup path")

    settings = get_settings()
    deadline = _Deadline(settings.LINKUP_PROXY_DEADLINE_SECONDS)
    max_response_bytes = settings.LINKUP_PROXY_MAX_RESPONSE_BYTES
    started = time.perf_counter()
    cleaned = path.lstrip("/")
    kind = cleaned.split("/", 1)[0]
    rest = cleaned.split("/", 1)[1] if "/" in cleaned else ""
    bound_poll = method.upper() == "GET" and kind in {"research", "extract"} and bool(rest)
    state = _CallState()
    scope = asyncio.timeout(deadline.remaining())
    try:
        # One scope covers key selection, rotation, retries, fallback, polls and streaming.
        async with scope:
            result = await _proxy_linkup(
                method=method,
                path=path,
                headers=headers,
                content=content,
                deadline=deadline,
                max_response_bytes=max_response_bytes,
                state=state,
            )
            deadline.check()
    except TimeoutError as exc:
        if not scope.expired():
            raise
        failure = LinkupDeadlineExceededError(bytes_read=deadline.bytes_read)
        if not bound_poll:
            await _record_failure(
                failure,
                agent_id=agent_id,
                method=method,
                path=path,
                content=content,
                key_id=state.key_id,
                started=started,
                quota_rotated=state.quota_rotated,
            )
        raise failure from exc
    except (LinkupProxyError, LinkupLimitError) as exc:
        if not bound_poll:
            await _record_failure(
                exc,
                agent_id=agent_id,
                method=method,
                path=path,
                content=content,
                key_id=state.key_id,
                started=started,
                quota_rotated=state.quota_rotated,
            )
        raise
    except Exception:
        # A completed billed response still counts if persisting its job binding fails.
        if state.result is not None and not bound_poll:
            await _record_result(
                state.result,
                agent_id=agent_id,
                method=method,
                path=path,
                content=content,
                key_id=state.key_id,
                started=started,
                quota_rotated=state.quota_rotated,
            )
        raise
    if bound_poll:
        return result
    # Analytics runs after the deadline scope has exited, even for failures.
    return await _record_result(
        result,
        agent_id=agent_id,
        method=method,
        path=path,
        content=content,
        key_id=state.key_id,
        started=started,
        quota_rotated=state.quota_rotated,
    )


async def _proxy_linkup(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    content: bytes | None,
    deadline: _Deadline,
    max_response_bytes: int,
    state: _CallState,
) -> tuple[int, str, dict[str, str]]:
    await ensure_env_key_seeded()
    deadline.check()
    cleaned = path.lstrip("/")
    kind, _, rest = cleaned.partition("/")
    url = f"{UPSTREAM_BASE}/v1/{cleaned}"

    if method.upper() == "GET" and kind in {"research", "extract"} and rest:
        try:
            bound = await key_for_job(rest.split("/", 1)[0])
        except LinkupJobKeyRemovedError as exc:
            raise LinkupProxyError(410, str(exc)) from exc
        status, body, out_headers = await _send_or_unavailable(
            method=method,
            url=url,
            headers=headers,
            content=content,
            api_key=decrypt_api_key(bound),
            deadline=deadline,
            max_response_bytes=max_response_bytes,
        )
        return status, body, out_headers

    start = await current_key()
    if start is None:
        env_key = await _env_fallback_key()
        if env_key is None:
            raise LinkupProxyError(503, "no Linkup API keys configured")
        return await _send_or_unavailable(
            method=method,
            url=url,
            headers=headers,
            content=content,
            api_key=env_key,
            deadline=deadline,
            max_response_bytes=max_response_bytes,
        )

    seen: set[UUID] = set()
    record = start
    last: tuple[int, str, dict[str, str]] | None = None
    transient_retries = 0

    while record is not None and record.id not in seen:
        # Stop before touching or trying another key. Local failures leave the ring alone.
        deadline.check()
        seen.add(record.id)
        state.key_id = record.id
        await touch_used(record.id)
        status, body, out_headers = await _send_or_unavailable(
            method=method,
            url=url,
            headers=headers,
            content=content,
            api_key=decrypt_api_key(record),
            deadline=deadline,
            max_response_bytes=max_response_bytes,
        )
        last = (status, body, out_headers)
        deadline.check()

        if is_quota_error(status, body):
            await mark_exhausted(record.id, message=body[:200])
            deadline.bytes_read = 0
            deadline.check()
            record = await advance_cursor(record.id)
            state.quota_rotated = True
            transient_retries = 0
            continue

        if is_transient_error(status) and transient_retries < 1:
            transient_retries += 1
            deadline.bytes_read = 0
            seen.discard(record.id)
            continue

        if status < 400 and kind in {"research", "extract"} and method.upper() == "POST":
            job_id = _extract_job_id(body)
            if job_id not in (None, ""):
                state.result = last
                await bind_job(upstream_job_id=job_id, key_id=record.id, kind=kind)
        return last

    if last is None:
        raise LinkupProxyError(503, "no Linkup API keys configured")
    return last

"""Forward Tavily calls with three retries per key and one-cycle failover within the deadline and response ceiling."""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from dataclasses import dataclass
from typing import final
from uuid import UUID

import httpx
from tenacity import AsyncRetrying, RetryError, retry_if_exception_type, retry_if_result, stop_after_attempt

from app.config import get_settings
from app.services.tavily.errors import (
    TavilyDeadlineExceededError,
    TavilyLimitError,
    TavilyResponseTooLargeError,
    is_quota_error,
)
from app.services.tavily.http_client import tavily_client
from app.services.tavily.keys import (
    DuplicateTavilyKeyError,
    add_key,
    advance_cursor,
    current_key,
    decrypt_api_key,
    ensure_env_key_seeded,
    mark_exhausted,
    touch_used,
)

# Tavily's REST API has no version prefix: POST /search and POST /crawl.
# The guest-facing proxy keeps its /api/tavily/v1 mount; the version segment never reaches the upstream.
UPSTREAM_BASE = "https://api.tavily.com"
SUBMIT_PATHS = frozenset({"search", "crawl"})
_PHASE_TIMEOUT_SECONDS = 60.0


@final
class TavilyProxyError(Exception):
    status_code: int
    body: str

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(body)
        self.status_code = status_code
        self.body = body


def allowed_upstream_path(path: str) -> bool:
    """Allow exactly the supported Tavily endpoints: search and crawl."""
    return path.lstrip("/") in SUBMIT_PATHS


def _monotonic() -> float:
    return time.monotonic()


@final
class _Deadline:
    """One monotonic budget for a whole proxied call: rotation, retry and env fallback."""

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
            raise TavilyDeadlineExceededError(bytes_read=self.bytes_read)


@dataclass(slots=True)
class _CallState:
    """Accumulate attribution across attempts for the single analytics write."""

    key_id: UUID | None = None
    quota_rotated: bool = False


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
    deadline.check()
    outbound = {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"host", "authorization", "content-length", "transfer-encoding"}
    }
    outbound["Authorization"] = f"Bearer {api_key}"
    deadline.bytes_read = 0
    # The client is shared and long-lived: never `async with` it, and bound every hop on the request itself so
    # waiting for a pooled connection counts against the call deadline.
    client = tavily_client()
    request = client.build_request(method, url, headers=outbound, content=content)
    redirects = 0
    while True:
        deadline.check()
        if redirects > client.max_redirects:
            raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)
        deadline.bytes_read = 0
        timeout = httpx.Timeout(min(_PHASE_TIMEOUT_SECONDS, deadline.remaining()))
        request.extensions = {**request.extensions, "timeout": timeout.as_dict()}
        # HTTPX builds the next request (method, auth, cookies), but must not buffer redirect bodies itself.
        async with aclosing(await client.send(request, stream=True, follow_redirects=False)) as response:
            chunks: list[bytes] = []
            async for chunk in response.aiter_bytes():
                deadline.bytes_read += len(chunk)
                if deadline.bytes_read > max_response_bytes:
                    raise TavilyResponseTooLargeError(bytes_read=deadline.bytes_read)
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


async def _env_fallback_key() -> str | None:
    env_key = get_settings().TAVILY_API_KEY.strip()
    if not env_key:
        return None
    try:
        record = await add_key(label="Environment TAVILY_API_KEY", api_key=env_key)
        return decrypt_api_key(record)
    except DuplicateTavilyKeyError:
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
    local_failure: TavilyLimitError | None = None,
    tenant_id: UUID | None = None,
) -> tuple[int, str, dict[str, str]]:
    from app.services.tavily.analytics import record_tavily_call

    status, body, _headers = result
    await record_tavily_call(
        agent_id=agent_id,
        tenant_id=tenant_id,
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
    exc: TavilyProxyError | TavilyLimitError,
    *,
    agent_id: UUID | None,
    method: str,
    path: str,
    content: bytes | None,
    key_id: UUID | None,
    started: float,
    quota_rotated: bool = False,
    tenant_id: UUID | None = None,
) -> None:
    _ = await _record_result(
        (exc.status_code, exc.body, {}),
        agent_id=agent_id,
        tenant_id=tenant_id,
        method=method,
        path=path,
        content=content,
        key_id=key_id,
        started=started,
        quota_rotated=quota_rotated,
        local_failure=exc if isinstance(exc, TavilyLimitError) else None,
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
    except httpx.HTTPError as exc:
        # A transport error that lands after the budget is gone is the deadline's doing, not the upstream's.
        if deadline.expired():
            raise TavilyDeadlineExceededError(bytes_read=deadline.bytes_read) from exc
        raise TavilyProxyError(502, "Tavily upstream unavailable") from exc


async def proxy_tavily(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    content: bytes | None,
    agent_id: UUID | None = None,
    tenant_id: UUID | None = None,
) -> tuple[int, str, dict[str, str]]:
    if not allowed_upstream_path(path):
        raise TavilyProxyError(404, "Unknown Tavily path")
    if method.upper() != "POST":
        raise TavilyProxyError(405, "Tavily path does not allow this method")

    settings = get_settings()
    deadline = _Deadline(settings.TAVILY_PROXY_DEADLINE_SECONDS)
    max_response_bytes = settings.TAVILY_PROXY_MAX_RESPONSE_BYTES
    started = time.perf_counter()
    state = _CallState()
    scope = asyncio.timeout(deadline.remaining())
    try:
        # One scope covers key selection, rotation, retries, fallback and streaming.
        async with scope:
            result = await _proxy_tavily(
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
        failure = TavilyDeadlineExceededError(bytes_read=deadline.bytes_read)
        await _record_failure(
            failure,
            agent_id=agent_id,
            tenant_id=tenant_id,
            method=method,
            path=path,
            content=content,
            key_id=state.key_id,
            started=started,
            quota_rotated=state.quota_rotated,
        )
        raise failure from exc
    except (TavilyProxyError, TavilyLimitError) as exc:
        await _record_failure(
            exc,
            agent_id=agent_id,
            tenant_id=tenant_id,
            method=method,
            path=path,
            content=content,
            key_id=state.key_id,
            started=started,
            quota_rotated=state.quota_rotated,
        )
        raise
    # Analytics runs after the deadline scope has exited, even for failures.
    return await _record_result(
        result,
        agent_id=agent_id,
        tenant_id=tenant_id,
        method=method,
        path=path,
        content=content,
        key_id=state.key_id,
        started=started,
        quota_rotated=state.quota_rotated,
    )


async def _proxy_tavily(
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
    url = f"{UPSTREAM_BASE}/{cleaned}"

    record = await current_key()

    def upstream_failed(result: tuple[int, str, dict[str, str]]) -> bool:
        return result[0] >= 400

    seen: set[UUID] = set()
    last: tuple[int, str, dict[str, str]] | TavilyProxyError | None = None

    while True:
        # Stop before touching or trying another key. Local failures leave the ring alone.
        deadline.check()
        if record is None:
            api_key = await _env_fallback_key()
            if api_key is None:
                raise TavilyProxyError(503, "no Tavily API keys configured")
        else:
            seen.add(record.id)
            state.key_id = record.id
            await touch_used(record.id)
            api_key = decrypt_api_key(record)
        quota_error = False
        result: tuple[int, str, dict[str, str]] | None = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),
                retry=retry_if_result(upstream_failed) | retry_if_exception_type(TavilyProxyError),
                reraise=True,
            ):
                with attempt:
                    result = await _send_or_unavailable(
                        method=method,
                        url=url,
                        headers=headers,
                        content=content,
                        api_key=api_key,
                        deadline=deadline,
                        max_response_bytes=max_response_bytes,
                    )
                outcome = attempt.retry_state.outcome
                if outcome is not None and not outcome.failed:
                    attempt.retry_state.set_result(result)
            last = result
        except RetryError:
            last = result
        except TavilyProxyError as exc:
            last = exc
        deadline.check()
        if record is None:
            break
        if isinstance(last, tuple):
            status, body, _out_headers = last
            if status < 400:
                return last
            if is_quota_error(status):
                await mark_exhausted(record.id, message=body[:200])
                quota_error = True

        deadline.bytes_read = 0
        deadline.check()
        record = await advance_cursor(record.id)
        if quota_error:
            state.quota_rotated = True
        if record is None or record.id in seen:
            break

    if isinstance(last, TavilyProxyError):
        raise last
    if last is None:
        raise TavilyProxyError(503, "no Tavily API keys configured")
    return last

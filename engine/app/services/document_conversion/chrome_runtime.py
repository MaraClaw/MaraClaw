"""Async, local-only Chrome DevTools helpers for document conversion."""

import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Protocol, Self
from urllib.parse import urlparse

import httpx

from app.core.json_types import (
    JsonObject,
    json_as_str,
    json_loads_object,
    json_object_from,
    json_object_from_response,
)
from app.services.document_conversion.resource_policy import ResourcePolicy


def trusted_executable(candidate: str | Path | None) -> Path | None:
    """Return an executable absolute path, or None when it is not launchable."""
    if candidate is None or not candidate:
        return None
    path = Path(candidate)
    if not path.is_absolute() or not path.is_file() or not path.stat().st_mode & 0o111:
        return None
    return path


class _InetSocket(Protocol):
    def getsockname(self) -> tuple[str, int]: ...


def _port_from_inet(sock: _InetSocket) -> int:
    _host, port = sock.getsockname()
    return port


def local_ephemeral_port() -> int:
    """Bind an unused localhost TCP port and return its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return _port_from_inet(sock)


def chrome_arguments(executable: Path, port: int, profile_path: str) -> list[str]:
    """Build the fixed argument vector used to launch local Chrome."""
    arguments = [
        str(executable),
        "--headless=new",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_path}",
        "about:blank",
    ]
    if sys.platform.startswith("linux"):
        arguments.extend(["--no-sandbox", "--disable-setuid-sandbox"])
    if sys.platform == "darwin":
        # HOME is the temporary profile, so the login keychain is unreachable; without this, loads stall.
        arguments.append("--use-mock-keychain")
    return arguments


_INHERITED_CHROME_ENVIRONMENT = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "FONTCONFIG_FILE",
    "FONTCONFIG_PATH",
)


def chrome_environment(profile_path: str) -> dict[str, str]:
    """Build the allowlisted environment for a Chrome child; nothing else is inherited."""
    environment = {name: os.environ[name] for name in _INHERITED_CHROME_ENVIRONMENT if name in os.environ}
    _ = environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    environment.update(
        {
            "HOME": profile_path,
            "TMPDIR": profile_path,
            "XDG_CONFIG_HOME": str(Path(profile_path) / "config"),
            "XDG_CACHE_HOME": str(Path(profile_path) / "cache"),
            "XDG_DATA_HOME": str(Path(profile_path) / "data"),
        }
    )
    return environment


async def launch_chrome(executable: Path, port: int, profile_path: str) -> asyncio.subprocess.Process:
    """Start local headless Chrome with the fixed arguments and the allowlisted environment."""
    return await asyncio.create_subprocess_exec(
        *chrome_arguments(executable, port, profile_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=chrome_environment(profile_path),
    )


def validate_debugger_websocket_url(url: str, port: int) -> str | None:
    """Accept only the DevTools websocket opened by this local Chrome process."""
    parsed = urlparse(url)
    try:
        expected_port = parsed.port == port
    except ValueError:
        return None
    if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or not expected_port:
        return None
    if not parsed.path.startswith("/devtools/"):
        return None
    return url


async def wait_for_cdp(port: int, deadline_seconds: float = 8) -> bool:
    """Wait until the local Chrome DevTools endpoint responds."""
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    async with httpx.AsyncClient(timeout=0.25, trust_env=False) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(endpoint)
                _ = response.raise_for_status()
                response.json()
                return True
            except httpx.HTTPError:
                await asyncio.sleep(0.1)
    return False


async def create_cdp_target(port: int) -> str | None:
    """Create an empty ``about:blank`` CDP target; the document is navigated only once it is policed."""
    endpoint = f"http://127.0.0.1:{port}/json/new?about:blank"
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        response = await client.put(endpoint)
        _ = response.raise_for_status()
    return json_as_str(json_object_from_response(response).get("webSocketDebuggerUrl"))


class CdpConnection(Protocol):
    async def send(self, message: str, /) -> None: ...

    async def recv(self) -> str | bytes: ...


class PolicedCdpSession:
    """A DevTools page session whose every request is checked against a ``ResourcePolicy``.

    Entering the session starts a reader task and enables ``Fetch`` interception
    for all URLs before returning, so nothing the page loads afterwards, its own
    document included, escapes the policy. The reader answers each
    ``Fetch.requestPaused`` event as it arrives, also while ``send`` is waiting
    for the reply to another command: allowed requests continue and refused ones
    fail with ``BlockedByClient``, so the page finishes loading without them.
    """

    def __init__(self, connection: CdpConnection, policy: ResourcePolicy, reply_timeout: float) -> None:
        self._connection: CdpConnection = connection
        self._policy: ResourcePolicy = policy
        self._reply_timeout: float = reply_timeout
        self._last_id: int = 0
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._loaded: asyncio.Event = asyncio.Event()
        self._reader: asyncio.Task[None] | None = None
        self.blocked_urls: list[str] = []

    async def __aenter__(self) -> Self:
        self._reader = asyncio.create_task(self._read())
        try:
            _ = await self.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._reader is None:
            return
        _ = self._reader.cancel()
        _ = await asyncio.gather(self._reader, return_exceptions=True)

    async def send(self, method: str, params: JsonObject | None = None) -> JsonObject:
        """Send a command and wait for its reply while paused requests keep being answered."""
        if self._reader is not None and self._reader.done():
            raise ConnectionError("DevTools connection is closed")
        reply = asyncio.Future[JsonObject]()
        message_id = self._next_id()
        self._pending[message_id] = reply
        try:
            await self._connection.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
            return await asyncio.wait_for(reply, timeout=self._reply_timeout)
        finally:
            _ = self._pending.pop(message_id, None)

    async def navigate(self, url: str, load_deadline_seconds: float) -> None:
        """Navigate the policed page and wait for its load event or the deadline."""
        if self._policy.permitted_url(url) is None:
            raise ValueError("Conversion source is outside the conversion workspace")
        if self._reader is None:
            raise RuntimeError("PolicedCdpSession must be entered before navigating")
        self._loaded.clear()
        _ = await self.send("Page.navigate", {"url": url})
        load_waiter = asyncio.create_task(self._loaded.wait())
        try:
            _ = await asyncio.wait(
                {load_waiter, self._reader},
                timeout=load_deadline_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            _ = load_waiter.cancel()
        if self._reader.done():
            self._reader.result()
            raise ConnectionError("DevTools connection closed while the page was loading")

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    async def _read(self) -> None:
        try:
            while True:
                await self._dispatch(json_loads_object(await self._connection.recv()))
        except Exception as exc:
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_exception(exc)
            raise

    async def _dispatch(self, message: JsonObject) -> None:
        method = message.get("method")
        if method == "Fetch.requestPaused":
            await self._answer_paused_request(json_object_from(message.get("params")))
            return
        if method == "Page.loadEventFired":
            self._loaded.set()
            return
        reply_id = message.get("id")
        reply = self._pending.get(reply_id) if isinstance(reply_id, int) else None
        if reply is not None and not reply.done():
            reply.set_result(message)

    async def _answer_paused_request(self, params: JsonObject) -> None:
        request_id = json_as_str(params.get("requestId"))
        if request_id in (None, ""):
            return
        url = json_as_str(json_object_from(params.get("request")).get("url")) or ""
        if self._policy.permitted_url(url) is not None:
            answer: JsonObject = {
                "id": self._next_id(),
                "method": "Fetch.continueRequest",
                "params": {"requestId": request_id},
            }
        else:
            self.blocked_urls.append(url)
            answer = {
                "id": self._next_id(),
                "method": "Fetch.failRequest",
                "params": {"requestId": request_id, "errorReason": "BlockedByClient"},
            }
        await self._connection.send(json.dumps(answer))


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a child process and wait for it before returning."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        _ = await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        process.kill()
        _ = await process.wait()


async def cleanup_temporary_paths(paths: list[Path]) -> None:
    """Remove conversion artifacts after their consumer has finished."""
    _ = await asyncio.gather(*(asyncio.to_thread(path.unlink, missing_ok=True) for path in paths))


async def write_temporary_bytes(data: bytes, suffix: str) -> Path:
    """Write a conversion artifact without blocking the event loop."""

    def write() -> Path:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary_file:
            _ = temporary_file.write(data)
            return Path(temporary_file.name)

    return await asyncio.to_thread(write)

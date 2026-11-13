"""A conversion workspace plus every reference an HTML renderer must refuse or allow."""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.metadata
import importlib.util
import io
import json
import sys
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from urllib.parse import urljoin

import pytest
import websockets
from PIL import Image

from app.core.json_types import JsonObject, json_as_int, json_as_str, json_loads_object, json_object_from

HTTP_PORT = 9


def png_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass(frozen=True)
class ContainmentWorkspace:
    workspace: Path
    page: Path
    inside_image: Path
    inside_png: bytes
    outside_png: bytes
    data_url: str
    blocked_references: tuple[str, ...]

    def page_url(self, reference: str) -> str:
        """The absolute URL Chrome requests for ``reference`` written in the page."""
        return urljoin(self.page.as_uri(), reference)


def build_containment_workspace(tmp_path: Path) -> ContainmentWorkspace:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_png = png_bytes((255, 0, 0))
    secret_image = outside / "secret.png"
    _ = secret_image.write_bytes(outside_png)
    _ = (outside / "secret.html").write_text("<p>OUTSIDE-SECRET</p>", encoding="utf-8")

    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    inside_png = png_bytes((0, 0, 255))
    inside_image = workspace / "pic.png"
    _ = inside_image.write_bytes(inside_png)
    (workspace / "escape.png").symlink_to(secret_image)
    page = workspace / "sub" / "page.html"
    _ = page.write_text("<p>page</p>", encoding="utf-8")

    blocked = (
        secret_image.as_uri(),
        str(secret_image),
        "../../outside/secret.png",
        "%2e%2e/%2e%2e/outside/secret.png",
        "..%2f..%2foutside%2fsecret.png",
        "../escape.png",
        "file:///proc/self/environ",
        "/proc/self/environ",
        f"http://127.0.0.1:{HTTP_PORT}/secret.png",
        f"https://127.0.0.1:{HTTP_PORT}/secret.png",
        f"ftp://127.0.0.1:{HTTP_PORT}/secret.png",
        f"ws://127.0.0.1:{HTTP_PORT}/socket",
        f"wss://127.0.0.1:{HTTP_PORT}/socket",
        f"//127.0.0.1:{HTTP_PORT}/secret.png",
    )
    return ContainmentWorkspace(
        workspace=workspace,
        page=page,
        inside_image=inside_image.resolve(),
        inside_png=inside_png,
        outside_png=outside_png,
        data_url="data:image/png;base64," + base64.b64encode(inside_png).decode(),
        blocked_references=blocked,
    )


class FakeDevToolsPage:
    """Scripted DevTools page target.

    Sending ``trigger_method`` makes the page pause one request per URL in
    ``request_urls`` and hold that command's reply until every paused request is
    answered, the way Chrome holds ``Page.navigate`` until its document request
    is decided. A session that only reads while waiting for its own reply would
    deadlock here instead of finishing.
    """

    def __init__(
        self,
        request_urls: list[str],
        *,
        trigger_method: str = "Page.navigate",
        results: dict[str, JsonObject] | None = None,
        close_on_trigger: bool = False,
    ) -> None:
        self.commands: list[JsonObject] = []
        self.decisions: dict[str, JsonObject] = {}
        self.load_event_sent: bool = False
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self._request_urls: list[str] = request_urls
        self._trigger_method: str = trigger_method
        self._results: dict[str, JsonObject] = results or {}
        self._close_on_trigger: bool = close_on_trigger
        self._paused: dict[str, str] = {}
        self._held_reply: int | None = None

    def methods(self) -> list[str]:
        return [str(command.get("method")) for command in self.commands]

    async def send(self, message: str, /) -> None:
        command = json_loads_object(message)
        self.commands.append(command)
        method = json_as_str(command.get("method"))
        command_id = json_as_int(command.get("id"))
        params = json_object_from(command.get("params"))
        if method == self._trigger_method:
            if self._close_on_trigger:
                self._incoming.put_nowait(None)
                return
            self._held_reply = command_id
            for index, url in enumerate(self._request_urls):
                request_id = f"request-{index}"
                self._paused[request_id] = url
                self._emit(
                    {"method": "Fetch.requestPaused", "params": {"requestId": request_id, "request": {"url": url}}}
                )
            self._release_when_answered()
            return
        if method in ("Fetch.continueRequest", "Fetch.failRequest"):
            url = self._paused.pop(json_as_str(params.get("requestId")) or "")
            self.decisions[url] = {"method": method, **params}
            self._emit({"id": command_id, "result": {}})
            self._release_when_answered()
            return
        self._emit({"id": command_id, "result": self._results.get(method or "", {})})

    async def recv(self) -> str:
        message = await self._incoming.get()
        if message is None:
            raise ConnectionError("DevTools page closed")
        return message

    def _emit(self, message: JsonObject) -> None:
        self._incoming.put_nowait(json.dumps(message))

    def _release_when_answered(self) -> None:
        if self._paused or self._held_reply is None:
            return
        self._emit({"id": self._held_reply, "result": self._results.get(self._trigger_method, {})})
        self._held_reply = None
        if self._trigger_method == "Page.navigate":
            self.load_event_sent = True
            self._emit({"method": "Page.loadEventFired", "params": {}})


@dataclass
class ChromeLaunch:
    argv: list[str]
    env: dict[str, str]


class FakeChromeProcess:
    returncode: int | None = None


@dataclass
class FakeChrome:
    """Replaces the process, DevTools HTTP endpoint and websocket used by a Chrome launch site."""

    page: FakeDevToolsPage
    launches: list[ChromeLaunch]
    created_targets: list[int]
    terminated: list[FakeChromeProcess]


def install_fake_chrome(monkeypatch: pytest.MonkeyPatch, module: str, page: FakeDevToolsPage) -> FakeChrome:
    """Route ``module``'s Chrome launch through the real ``launch_chrome`` into fakes."""
    fake = FakeChrome(page=page, launches=[], created_targets=[], terminated=[])
    port = 9333

    async def create_subprocess_exec(*argv: str, **kwargs: object) -> FakeChromeProcess:
        env = kwargs.get("env")
        fake.launches.append(ChromeLaunch(list(argv), dict(env) if isinstance(env, dict) else {}))
        return FakeChromeProcess()

    async def wait_for_cdp(_port: int) -> bool:
        return True

    async def create_cdp_target(target_port: int) -> str:
        fake.created_targets.append(target_port)
        return f"ws://127.0.0.1:{target_port}/devtools/page/fake"

    async def terminate_process(process: FakeChromeProcess) -> None:
        fake.terminated.append(process)

    @asynccontextmanager
    async def connect(url: str, **_: object) -> AsyncGenerator[FakeDevToolsPage]:
        assert url == f"ws://127.0.0.1:{port}/devtools/page/fake"
        yield page

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(f"{module}.chrome_executable", lambda: sys.executable)
    monkeypatch.setattr(f"{module}.local_ephemeral_port", lambda: port)
    monkeypatch.setattr(f"{module}.wait_for_cdp", wait_for_cdp)
    monkeypatch.setattr(f"{module}.create_cdp_target", create_cdp_target)
    monkeypatch.setattr(f"{module}.terminate_process", terminate_process)
    monkeypatch.setattr(websockets, "connect", connect)
    return fake


@contextmanager
def real_weasyprint_urls() -> Generator[ModuleType]:
    """Load the installed WeasyPrint ``urls`` module without the package's native (pango) imports."""
    saved = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "weasyprint"}
    for name in saved:
        del sys.modules[name]
    spec = importlib.util.find_spec("weasyprint")
    if spec is None or spec.submodule_search_locations is None:
        raise RuntimeError("weasyprint is not installed")
    package = ModuleType("weasyprint")
    package.__path__ = list(spec.submodule_search_locations)
    package.__dict__["__version__"] = importlib.metadata.version("weasyprint")
    sys.modules["weasyprint"] = package
    try:
        yield importlib.import_module("weasyprint.urls")
    finally:
        for name in [name for name in sys.modules if name.split(".")[0] == "weasyprint"]:
            del sys.modules[name]
        sys.modules.update(saved)

import base64
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO

import pytest
from agentbay import BrowserOption, CreateSessionParams
from PIL import Image

from app.services import agent_tools, agentbay_client, take_control_state
from take_control_fakes import SharedRedis


@dataclass(slots=True)
class Remote:
    session_id: str
    page: str = "https://portal.example/login"
    deleted: bool = False
    files: dict[str, str] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CommandResult:
    success: bool = True
    output: str = ""
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""
    exit_code: int = 0


class Browser:
    def __init__(self, store: SDKStore, remote: Remote) -> None:
        self.store: SDKStore = store
        self.remote: Remote = remote
        self.initializations: int = 0
        self.operator: Browser = self

    def initialize(self, _options: BrowserOption) -> bool:
        self.store.before_io()
        self.initializations += 1
        return True

    def screenshot(self, *, full_page: bool) -> bytes:
        self.store.before_io()
        assert full_page is False
        buffer = BytesIO()
        Image.new("RGB", (2, 2)).save(buffer, format="PNG")
        return buffer.getvalue()


class Command:
    def __init__(self, store: SDKStore, remote: Remote) -> None:
        self.store: SDKStore = store
        self.remote: Remote = remote

    def exec(self, command: str, **_kwargs: object) -> CommandResult:
        self.store.before_io()
        if command.startswith("echo '"):
            encoded = command.split("'", 2)[1]
            path = command.rsplit(" > ", 1)[1]
            self.remote.files[path] = base64.b64decode(encoded).decode()
        if command.startswith("node "):
            path = command.split(";", 1)[0].split()[1]
            script = self.remote.files[path]
            self.remote.executed.append(path)
            if "URL_OK:" in script:
                return CommandResult(output=f"URL_OK:{self.remote.page}")
            if "COOKIES_EXPORT:" in script:
                return CommandResult(stdout='COOKIES_EXPORT:[{"name":"session","value":"secret"}]')
            marker = re.search(r"console\.log\('(CLICK_OK|TYPE_OK|PRESS_OK|TC_OK)[^']*'\)", script)
            assert marker is not None
            return CommandResult(output=marker[1])
        assert re.match(r"echo '[A-Za-z0-9+/=]+'", command)
        return CommandResult()


@dataclass(frozen=True, slots=True)
class ScreenSize:
    success: bool = True
    data: tuple[int, int] = (1920, 1080)


class Computer:
    def get_screen_size(self) -> ScreenSize:
        return ScreenSize()


class Session:
    def __init__(self, store: SDKStore, remote: Remote) -> None:
        self.session_id: str = remote.session_id
        self.remote: Remote = remote
        self.browser: Browser = Browser(store, remote)
        self.command: Command = Command(store, remote)
        self.computer: Computer = Computer()

    def delete(self) -> None:
        self.remote.deleted = True


@dataclass(frozen=True, slots=True)
class SessionResult:
    session: Session | None
    success: bool = True
    error_message: str = ""
    code: str = ""


class SDK:
    def __init__(self, store: SDKStore) -> None:
        self.store: SDKStore = store

    def create(self, _params: CreateSessionParams) -> SessionResult:
        remote = Remote(f"remote-{len(self.store.remotes)}")
        self.store.remotes.append(remote)
        return SessionResult(Session(self.store, remote))

    def get(self, session_id: str) -> SessionResult:
        self.store.before_io()
        self.store.attached.append(session_id)
        remote = next(remote for remote in self.store.remotes if remote.session_id == session_id)
        if remote.deleted:
            return SessionResult(None, success=False, code="InvalidMcpSession.NotFound", error_message="released")
        return SessionResult(Session(self.store, remote))


class SDKStore:
    def __init__(self) -> None:
        self.remotes: list[Remote] = []
        self.attached: list[str] = []
        self.before_io: Callable[[], None] = lambda: None

    def sdk(self, *, api_key: str) -> SDK:
        assert api_key == "akm-test"
        return SDK(self)


def install(monkeypatch: pytest.MonkeyPatch, redis: SharedRedis) -> SDKStore:
    store = SDKStore()

    async def connection() -> SharedRedis:
        return redis

    async def tool_config(_agent_id: uuid.UUID, _name: str) -> dict[str, str]:
        return {}

    async def api_key(_agent_id: uuid.UUID) -> str:
        return "akm-test"

    async def inject(_client: agentbay_client.AgentBayClient, _agent_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(take_control_state, "_connection", connection)
    monkeypatch.setattr(agentbay_client, "AgentBay", store.sdk)
    monkeypatch.setattr(agentbay_client, "_agentbay_sessions", {})
    monkeypatch.setattr(agentbay_client, "_inject_credentials", inject)
    monkeypatch.setattr(agent_tools, "_get_tool_config", tool_config)
    monkeypatch.setattr(agentbay_client, "get_agentbay_api_key_for_agent", api_key)
    return store

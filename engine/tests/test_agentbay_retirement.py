import importlib
import inspect
import uuid
from dataclasses import dataclass

import pytest
from agentbay import CreateSessionParams

from app.services import agent_tools, agentbay_client


def test_factory_rejects_missing_session_argument() -> None:
    # Given the factory's actual Python argument-binding contract.
    signature = inspect.signature(agentbay_client.get_agentbay_client_for_agent)
    # When a caller omits the session ID, then binding fails before any SDK work.
    with pytest.raises(TypeError, match="session_id"):
        signature.bind(uuid.uuid4(), "code")


def test_legacy_preview_module_cannot_be_imported() -> None:
    # Given the retired first-party module path.
    module_name = "app.services.agentbay_live"
    # When importing it, then the missing module itself is reported.
    with pytest.raises(ModuleNotFoundError) as failure:
        importlib.import_module(module_name)
    assert failure.value.name == module_name


@dataclass(frozen=True, slots=True)
class SDKSession:
    session_id: str


@dataclass(frozen=True, slots=True)
class CreateResult:
    session: SDKSession
    success: bool = True
    error_message: str = ""


@pytest.mark.parametrize(
    "lookup",
    [
        (True, "chat", "code", True),
        (True, "other-chat", "code", False),
        (True, "", "code", False),
        (False, "chat", "code", False),
        (True, "chat", "computer", False),
    ],
)
async def test_explicit_canonical_key_controls_client_isolation(
    monkeypatch: pytest.MonkeyPatch, lookup: tuple[bool, str, str, bool]
) -> None:
    # Given a real client wrapper with only DB configuration and the remote SDK faked.
    same_agent, session_id, image_type, reuse = lookup
    created_images: list[str | None] = []

    class SDK:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "akm-test"

        def create(self, params: CreateSessionParams) -> CreateResult:
            created_images.append(params.image_id)
            return CreateResult(SDKSession(f"remote-{len(created_images)}"))

    async def tool_config(_agent_id: uuid.UUID, _tool_name: str) -> dict[str, str]:
        return {}

    async def api_key(_agent_id: uuid.UUID) -> str:
        return "akm-test"

    monkeypatch.setattr(agentbay_client, "AgentBay", SDK)
    monkeypatch.setattr(agent_tools, "_get_tool_config", tool_config)
    monkeypatch.setattr(agentbay_client, "get_agentbay_api_key_for_agent", api_key)
    monkeypatch.setattr(agentbay_client, "_agentbay_sessions", {})
    agent_id = uuid.uuid4()
    original = await agentbay_client.get_agentbay_client_for_agent(agent_id, "code", session_id="chat")
    selected_agent = agent_id if same_agent else uuid.uuid4()

    # When looking up a fully explicit canonical key, including an explicit empty ID.
    selected = await agentbay_client.get_agentbay_client_for_agent(selected_agent, image_type, session_id=session_id)

    # Then only the identical key reuses the remote environment.
    assert (selected is original) is reuse
    expected_images = (
        ["linux_latest"]
        if reuse
        else ["linux_latest", "windows_latest" if image_type == "computer" else "linux_latest"]
    )
    assert created_images == expected_images

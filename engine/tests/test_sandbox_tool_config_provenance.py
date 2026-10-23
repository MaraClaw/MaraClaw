import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.json_types import JsonObject
from app.core.security import encrypt_data
from app.services import agent_tools
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.tool_runtime import tool_config
from app.services.tool_runtime.provenance import ConfigLayer, config_provenance

SECRET = "test-secret-key"
PLATFORM_URL = "http://10.0.0.5:8080/v1/shell/exec"
GLOBAL_URL = "http://10.0.0.9:9000/exec"
TENANT_URL = "https://tenant.example/exec"
TOOL_NAME = "execute_code"


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: int = 0

    async def execute(self, **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(success=True, stdout="", stderr="", exit_code=0, duration_ms=1, error=None)

    def _format_result(self, result: object) -> str:
        return "ok"


class Stored:
    """Stored layers for one agent; each is returned by the patched DAOs as the database would."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.global_config: JsonObject = {}
        self.tenant_config: JsonObject = {}
        self.agent_config: JsonObject = {}
        self.source: str = "builtin"
        self.tenant_error: Exception | None = None
        monkeypatch.setattr(tool_config, "get_settings", lambda: SimpleNamespace(SECRET_KEY=SECRET))
        monkeypatch.setattr(agent_tools, "_tool_config_cache", {})
        monkeypatch.setattr(
            tool_config.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(tenant_id=uuid.uuid4()))
        )
        monkeypatch.setattr(tool_config.agent_tool_dao, "get_assignment_with_tool_by_name", self._assignment)
        monkeypatch.setattr(tool_config, "get_tenant_tool_config", self._tenant)

    async def _assignment(self, agent_id: uuid.UUID, tool_name: str) -> tuple[SimpleNamespace, JsonObject]:
        fields: JsonObject = {
            "config": self.global_config,
            "config_schema": None,
            "source": self.source,
            "name": tool_name,
        }
        return SimpleNamespace(config=self.agent_config), fields

    async def _tenant(self, tenant_id: uuid.UUID | None, tool_name: str, schema: object) -> JsonObject:
        if self.tenant_error is not None:
            raise self.tenant_error
        return self.tenant_config


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> Stored:
    return Stored(monkeypatch)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> tuple[RecordingBackend, list[SandboxConfig]]:
    selected: list[SandboxConfig] = []
    recorder = RecordingBackend()

    def select(config: SandboxConfig) -> RecordingBackend:
        selected.append(config)
        return recorder

    platform = SandboxConfig(type=SandboxType.SELF_HOSTED, api_url=PLATFORM_URL, api_key="platform-secret")
    monkeypatch.setattr("app.config.get_sandbox_config", platform.grant_operator_endpoint)
    monkeypatch.setattr("app.services.sandbox.registry.get_sandbox_backend", select)
    return recorder, selected


async def test_loader_retains_provenance_before_merge_and_through_the_cache(stored: Stored) -> None:
    # Given: distinct layers, with the agent's credential encrypted at rest.
    agent_id = uuid.uuid4()
    stored.global_config = {"api_url": GLOBAL_URL, "shared": "global"}
    stored.tenant_config = {"api_url": TENANT_URL, "shared": "tenant"}
    stored.agent_config = {"api_key": encrypt_data("agent-key", SECRET), "shared": "agent"}
    # When: the facade loads the config twice.
    first = await agent_tools._get_tool_config(agent_id, TOOL_NAME)
    second = await agent_tools._get_tool_config(agent_id, TOOL_NAME)
    # Then: the merged JSON is unchanged, the cache returns the same object, and provenance names each writer.
    assert first == {"api_url": TENANT_URL, "shared": "agent", "api_key": "agent-key"}
    assert second is first
    provenance = config_provenance(second)
    assert provenance is not None
    assert {key: (source.layer, source.endpoint) for key, source in provenance.sources.items()} == {
        "api_url": (ConfigLayer.TENANT, TENANT_URL),
        "shared": (ConfigLayer.AGENT, TENANT_URL),
        "api_key": (ConfigLayer.AGENT, TENANT_URL),
    }


async def test_loader_marks_only_builtin_global_rows_as_operator_layer(stored: Stored) -> None:
    # Given: the same stored row once as built-in and once as a custom tool.
    stored.global_config = {"api_url": GLOBAL_URL}
    # When
    builtin = config_provenance(await agent_tools._get_tool_config(uuid.uuid4(), TOOL_NAME))
    stored.source = "custom"
    custom = config_provenance(await agent_tools._get_tool_config(uuid.uuid4(), TOOL_NAME))
    # Then
    assert builtin is not None
    assert custom is not None
    assert builtin.sources["api_url"].layer is ConfigLayer.BUILTIN_GLOBAL
    assert custom.sources["api_url"].layer is ConfigLayer.CUSTOM_GLOBAL


async def test_tenant_endpoint_override_reaches_the_backend_without_platform_key_or_authority(
    stored: Stored, backend: tuple[RecordingBackend, list[SandboxConfig]], tmp_path: Path
) -> None:
    # Given: a tenant that redirects the platform adapter and spoofs authority in its JSON.
    stored.tenant_config = {"api_url": TENANT_URL, "operator_url": TENANT_URL, "private": True}
    recorder, selected = backend
    # When: code runs through the real loader, cache facade and code-exec handler.
    result = await agent_tools._execute_code(uuid.uuid4(), tmp_path, {"code": "pass"})
    # Then: the backend receives the tenant endpoint with no platform key and no private authority.
    assert result == "ok"
    assert [(config.api_url, config.api_key, config.operator_url) for config in selected] == [(TENANT_URL, "", None)]
    assert recorder.calls == 1


async def test_tenant_pair_and_operator_endpoint_reach_the_backend_through_the_cache(
    stored: Stored, backend: tuple[RecordingBackend, list[SandboxConfig]], tmp_path: Path
) -> None:
    # Given: an operator endpoint in the built-in row and a tenant key issued for that endpoint.
    agent_id = uuid.uuid4()
    stored.global_config = {"api_url": GLOBAL_URL, "api_key": encrypt_data("global-key", SECRET)}
    stored.tenant_config = {"api_key": "tenant-key"}
    _, selected = backend
    # When: code runs twice, the second time from the cache.
    for _ in range(2):
        _ = await agent_tools._execute_code(agent_id, tmp_path, {"code": "pass"})
    # Then: both runs keep the tenant key and operator authority for that origin.
    assert [(config.api_url, config.api_key, config.operator_url) for config in selected] == [
        (GLOBAL_URL, "tenant-key", GLOBAL_URL)
    ] * 2


@pytest.mark.parametrize("layer", ["global", "agent", "tenant"])
async def test_decryption_failure_is_fixed_sanitized_and_never_falls_back(
    stored: Stored, backend: tuple[RecordingBackend, list[SandboxConfig]], tmp_path: Path, layer: str
) -> None:
    # Given: one layer holds a credential that cannot be decrypted, and the platform has its own key.
    recorder, selected = backend
    match layer:
        case "global":
            stored.global_config = {"api_key": "not-ciphertext-leak"}
        case "agent":
            stored.agent_config = {"api_key": "not-ciphertext-leak"}
        case _:
            stored.tenant_error = ValueError("Decryption failed: not-ciphertext-leak")
    # When
    result = await agent_tools._execute_code(uuid.uuid4(), tmp_path, {"code": "pass"})
    # Then: a fixed configuration error surfaces, with no detail, no backend and no platform-key fallback.
    assert result == "❌ Sandbox configuration error: Tool configuration credentials could not be decrypted"
    assert selected == []
    assert recorder.calls == 0

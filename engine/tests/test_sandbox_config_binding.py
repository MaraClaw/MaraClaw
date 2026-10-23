from collections.abc import Sequence

import pytest

from app.core.json_types import JsonObject
from app.services.agent_tool_exec.code_exec import _sandbox_overrides
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.tool_runtime.provenance import ConfigLayer, config_provenance, merge_layers

PLATFORM_URL = "http://10.0.0.5:8080/v1/shell/exec"
TENANT_URL = "https://tenant.example/exec"
GLOBAL_URL = "http://10.0.0.9:9000/exec"

Layers = Sequence[tuple[ConfigLayer, JsonObject]]


def platform_config() -> SandboxConfig:
    return SandboxConfig(
        type=SandboxType.SELF_HOSTED, api_url=PLATFORM_URL, api_key="platform-secret"
    ).grant_operator_endpoint()


def resolve(layers: Layers, platform: SandboxConfig | None = None) -> SandboxConfig:
    merged = merge_layers(layers)
    return SandboxConfig.from_dict(
        _sandbox_overrides(merged), platform or platform_config(), provenance=config_provenance(merged)
    )


def test_endpoint_override_does_not_inherit_platform_key() -> None:
    # Given: a platform key paired with the platform endpoint.
    # When: a plain mapping redirects the same backend to another endpoint without its own key.
    config = SandboxConfig.from_dict({"sandbox_type": "self_hosted", "api_url": TENANT_URL}, platform_config())
    # Then: the platform key does not follow the new destination.
    assert config.api_url == TENANT_URL
    assert config.api_key == ""


def test_explicit_plaintext_key_is_not_replaced_by_platform_key() -> None:
    # Given: the loader already decrypted the tenant's own key.
    # When: the tenant supplies an endpoint and key together.
    config = SandboxConfig.from_dict(
        {"sandbox_type": "self_hosted", "api_url": TENANT_URL, "api_key": "tenant-plain"}, platform_config()
    )
    # Then: the tenant's pair is kept without a second decryption attempt.
    assert config.api_key == "tenant-plain"


def test_platform_key_follows_the_platform_origin_only() -> None:
    # Given/When: a mapping that keeps the platform origin on another path, one that leaves it, and no override.
    same_origin = SandboxConfig.from_dict({"api_url": "http://10.0.0.5:8080/v1/jupyter/execute"}, platform_config())
    other_port = SandboxConfig.from_dict({"api_url": "http://10.0.0.5:9090/v1/shell/exec"}, platform_config())
    inherited = SandboxConfig.from_dict({"max_timeout": 3}, platform_config())
    # Then: only the platform origin carries the platform key.
    assert [same_origin.api_key, other_port.api_key, inherited.api_key] == ["platform-secret", "", "platform-secret"]


@pytest.mark.parametrize(
    ("layers", "expected_key"),
    [
        pytest.param(
            [
                (ConfigLayer.BUILTIN_GLOBAL, {"api_url": GLOBAL_URL, "api_key": "global-key"}),
                (ConfigLayer.TENANT, {"api_url": TENANT_URL}),
            ],
            "",
            id="tenant-endpoint-does-not-inherit-global-key",
        ),
        pytest.param(
            [
                (ConfigLayer.BUILTIN_GLOBAL, {"api_url": GLOBAL_URL, "api_key": "global-key"}),
                (ConfigLayer.TENANT, {"api_url": TENANT_URL, "api_key": "tenant-key"}),
            ],
            "tenant-key",
            id="explicit-tenant-pair-is-kept",
        ),
        pytest.param(
            [
                (ConfigLayer.TENANT, {"api_url": TENANT_URL, "api_key": "tenant-key"}),
                (ConfigLayer.AGENT, {"api_url": "https://agent.example/exec"}),
            ],
            "",
            id="agent-endpoint-does-not-inherit-tenant-key",
        ),
        pytest.param(
            [(ConfigLayer.TENANT, {"api_url": TENANT_URL}), (ConfigLayer.AGENT, {"api_key": "agent-key"})],
            "agent-key",
            id="agent-key-accompanies-the-endpoint-visible-to-it",
        ),
        pytest.param(
            [(ConfigLayer.BUILTIN_GLOBAL, {"api_key": "global-key"}), (ConfigLayer.TENANT, {"api_url": TENANT_URL})],
            "",
            id="key-issued-for-platform-endpoint-does-not-follow-tenant-endpoint",
        ),
        pytest.param(
            [(ConfigLayer.TENANT, {"api_key": "tenant-key"})],
            "tenant-key",
            id="key-without-endpoint-stays-with-the-platform-endpoint",
        ),
        pytest.param(
            [(ConfigLayer.BUILTIN_GLOBAL, {"api_key": "global-key"}), (ConfigLayer.AGENT, {"api_key": ""})],
            "platform-secret",
            id="blank-key-falls-back-to-platform-key-for-platform-endpoint",
        ),
        pytest.param(
            [(ConfigLayer.TENANT, {"api_url": TENANT_URL}), (ConfigLayer.AGENT, {"api_key": ""})],
            "",
            id="blank-key-does-not-pull-platform-key-to-tenant-endpoint",
        ),
    ],
)
def test_credentials_follow_only_the_origin_visible_where_they_were_issued(layers: Layers, expected_key: str) -> None:
    # Given: layered configuration merged with provenance captured before the merge.
    # When
    config = resolve(layers)
    # Then
    assert config.api_key == expected_key


@pytest.mark.parametrize(
    ("layers", "authorized"),
    [
        pytest.param([(ConfigLayer.BUILTIN_GLOBAL, {"api_url": GLOBAL_URL})], True, id="builtin-global-endpoint"),
        pytest.param(
            [(ConfigLayer.BUILTIN_GLOBAL, {"sandbox_type": "self_hosted"}), (ConfigLayer.AGENT, {"max_timeout": 3})],
            True,
            id="platform-endpoint-with-operator-type",
        ),
        pytest.param([(ConfigLayer.AGENT, {"max_timeout": 3})], True, id="unrelated-agent-field"),
        pytest.param([(ConfigLayer.CUSTOM_GLOBAL, {"api_url": GLOBAL_URL})], False, id="non-builtin-global-endpoint"),
        pytest.param([(ConfigLayer.TENANT, {"api_url": GLOBAL_URL})], False, id="tenant-endpoint"),
        pytest.param([(ConfigLayer.AGENT, {"api_url": PLATFORM_URL})], False, id="agent-copies-platform-endpoint"),
        pytest.param(
            [(ConfigLayer.BUILTIN_GLOBAL, {"api_url": GLOBAL_URL}), (ConfigLayer.TENANT, {"api_url": TENANT_URL})],
            False,
            id="tenant-overrides-operator-endpoint",
        ),
        pytest.param(
            [(ConfigLayer.BUILTIN_GLOBAL, {"api_url": GLOBAL_URL}), (ConfigLayer.AGENT, {"sandbox_type": "judge0"})],
            False,
            id="agent-repoints-adapter-at-operator-endpoint",
        ),
        pytest.param([(ConfigLayer.TENANT, {"sandbox_type": "judge0"})], False, id="tenant-repoints-platform-adapter"),
    ],
)
def test_private_authority_comes_only_from_operator_layers(layers: Layers, authorized: bool) -> None:
    # Given: the platform endpoint holds operator authority.
    # When
    config = resolve(layers)
    # Then: authority survives only when operator layers chose both endpoint and adapter.
    assert (config.operator_url is not None) is authorized
    assert config.operator_url in (None, config.api_url)


def test_plain_mapping_is_conservative() -> None:
    # Given: mappings without provenance, as produced by copies or test doubles.
    inherits = SandboxConfig.from_dict({"max_timeout": 3}, platform_config())
    copies_url = SandboxConfig.from_dict({"api_url": PLATFORM_URL}, platform_config())
    # Then: only values inherited from the platform keep its authority.
    assert inherits.operator_url == PLATFORM_URL
    assert copies_url.operator_url is None


def test_copying_a_loaded_mapping_drops_its_provenance() -> None:
    # Given: a loader-produced mapping that holds an operator endpoint.
    merged = merge_layers([(ConfigLayer.BUILTIN_GLOBAL, {"api_url": GLOBAL_URL})])
    copied = dict(merged)
    # When: a consumer copies it into a plain mapping.
    config = SandboxConfig.from_dict(_sandbox_overrides(copied), None, provenance=config_provenance(copied))
    # Then: the copy is untrusted.
    assert config_provenance(merged) is not None
    assert config.operator_url is None


@pytest.mark.parametrize(
    "spoof",
    [
        {"operator_url": GLOBAL_URL},
        {"_operator_url": GLOBAL_URL},
        {"grant_operator_endpoint": True, "operator": True, "private": True, "trusted": True},
        {"provenance": {"api_url": {"layer": "builtin_global", "endpoint": ""}}},
        {"layer": "builtin_global", "source": "builtin"},
    ],
)
def test_json_cannot_manufacture_authority(spoof: JsonObject) -> None:
    # Given: tenant JSON that claims operator authority alongside its own endpoint.
    layers: Layers = [(ConfigLayer.TENANT, {"api_url": GLOBAL_URL, **spoof})]
    # When
    config = resolve(layers)
    validated = SandboxConfig.model_validate({"api_url": GLOBAL_URL, **spoof})
    # Then: neither the loader path nor direct validation grants authority, and dumps carry none.
    assert config.operator_url is None
    assert validated.operator_url is None
    assert "operator_url" not in SandboxConfig(api_url=GLOBAL_URL).grant_operator_endpoint().model_dump()


def test_authority_survives_copy() -> None:
    # Given: an operator-granted configuration.
    # When
    copied = platform_config().model_copy()
    # Then: the copy keeps its authority.
    assert copied.operator_url == PLATFORM_URL

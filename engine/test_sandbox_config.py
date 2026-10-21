from pydantic import TypeAdapter

from app.services.sandbox.config import SandboxConfig, SandboxConfigOverrides

config_adapter = TypeAdapter[SandboxConfigOverrides](SandboxConfigOverrides)

config = config_adapter.validate_python({"allow_network": True})
fallback = SandboxConfig(allow_network=False)
result = SandboxConfig.from_dict(config, fallback)
print(f"Result 1: {result.allow_network}")

config2 = config_adapter.validate_python({"allow_network": "true"})
result2 = SandboxConfig.from_dict(config2, fallback)
print(f"Result 2: {result2.allow_network}")

config3 = config_adapter.validate_python({"allow_network": "1"})
result3 = SandboxConfig.from_dict(config3, fallback)
print(f"Result 3: {result3.allow_network}")

config4 = config_adapter.validate_python({})
result4 = SandboxConfig.from_dict(config4, fallback)
print(f"Result 4: {result4.allow_network}")

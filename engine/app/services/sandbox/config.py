"""Sandbox configuration models."""

from enum import StrEnum
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import logger


class SandboxConfigOverrides(TypedDict, total=False):
    """Supported per-agent sandbox configuration values."""

    sandbox_type: str
    api_key: str
    api_url: str
    cpu_limit: str
    memory_limit: str
    allow_network: bool
    allow_unsafe_fallback_when_bwrap_missing: bool
    default_timeout: int
    max_timeout: int
    http_proxy: str
    https_proxy: str
    no_proxy: str


class SandboxType(StrEnum):
    """Supported sandbox backend types."""

    SUBPROCESS = "subprocess"
    DOCKER = "docker"
    E2B = "e2b"
    JUDGE0 = "judge0"
    CODEDANDBOX = "codesandbox"
    SELF_HOSTED = "self_hosted"
    AIO_SANDBOX = "aio_sandbox"


class SandboxConfig(BaseModel):
    """Configuration for sandbox backend."""

    model_config = ConfigDict(use_enum_values=True)

    type: SandboxType = SandboxType.SUBPROCESS
    enabled: bool = True

    # Local sandbox options
    cpu_limit: str = "0.5"
    memory_limit: str = "256m"
    allow_network: bool = True
    allow_unsafe_fallback_when_bwrap_missing: bool = False

    # API sandbox options
    api_key: str = ""
    api_url: str = ""

    # Common options
    default_timeout: int = Field(default=30, ge=1, le=3600)
    max_timeout: int = Field(default=60, ge=1, le=3600)

    # Proxy options (explicit sandbox config only; not inherited from process env)
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None

    # Language mapping for API sandboxes
    # Maps our internal language names to API-specific language IDs
    language_mapping: dict[str, str] = Field(
        default_factory=lambda: {
            "python": "python",
            "bash": "bash",
            "node": "javascript",
            "javascript": "javascript",
        }
    )

    def resolve_proxy_env(self) -> dict[str, str]:
        """Build guest proxy env vars from config when network is allowed.

        Injection is gated on ``allow_network`` so disabled-network sandboxes
        cannot harvest proxy URLs/credentials from the environment. Only
        explicit SandboxConfig fields are used - no ``os.environ`` fallback.
        Both lowercase and uppercase keys are set for client compatibility.
        """
        if not self.allow_network:
            return {}
        env: dict[str, str] = {}
        if self.http_proxy:
            env["http_proxy"] = self.http_proxy
            env["HTTP_PROXY"] = self.http_proxy
        if self.https_proxy:
            env["https_proxy"] = self.https_proxy
            env["HTTPS_PROXY"] = self.https_proxy
        if self.no_proxy:
            env["no_proxy"] = self.no_proxy
            env["NO_PROXY"] = self.no_proxy
        return env

    @classmethod
    def from_dict(
        cls,
        config: SandboxConfigOverrides,
        fallback_config: SandboxConfig | None = None,
    ) -> SandboxConfig:
        """Build SandboxConfig from a dict with field-level fallback support.

        Args:
            config: Tool configuration dict.
            fallback_config: Fallback configuration, usually from environment variables.

        Returns:
            SandboxConfig instance.
        """

        def get_value[T](value: T | None, fallback: T) -> T:
            """Prefer a configured value and retain the existing empty-string fallback."""
            if value is None or value == "":
                return fallback
            return value

        # Map config key names to SandboxConfig attributes
        sandbox_type_str = get_value(
            config.get("sandbox_type"),
            SandboxType.SUBPROCESS.value,
        )
        try:
            sandbox_type = SandboxType(sandbox_type_str)
        except ValueError:
            sandbox_type = SandboxType.SUBPROCESS

        api_key = get_value(
            config.get("api_key"),
            fallback_config.api_key if fallback_config else "",
        )
        if api_key:
            try:
                from app.config import get_settings
                from app.core.security import decrypt_data

                settings = get_settings()
                api_key = decrypt_data(api_key, settings.SECRET_KEY)
            except Exception as error:
                logger.warning(f"[SandboxConfig] Failed to decrypt api_key: {error}")
                api_key = fallback_config.api_key if fallback_config else ""

        allow_network = get_value(
            config.get("allow_network"),
            fallback_config.allow_network if fallback_config else False,
        )
        logger.info(f"[SandboxConfig] allow_network: raw={config.get('allow_network')!r}, resolved={allow_network!r}")

        return cls(
            type=sandbox_type,
            enabled=True,  # Always enabled when explicitly configured
            api_key=api_key,
            api_url=get_value(config.get("api_url"), fallback_config.api_url if fallback_config else ""),
            cpu_limit=get_value(config.get("cpu_limit"), fallback_config.cpu_limit if fallback_config else "0.5"),
            memory_limit=get_value(
                config.get("memory_limit"),
                fallback_config.memory_limit if fallback_config else "256m",
            ),
            allow_network=allow_network,
            allow_unsafe_fallback_when_bwrap_missing=get_value(
                config.get("allow_unsafe_fallback_when_bwrap_missing"),
                fallback_config.allow_unsafe_fallback_when_bwrap_missing if fallback_config else False,
            ),
            default_timeout=get_value(
                config.get("default_timeout"),
                fallback_config.default_timeout if fallback_config else 30,
            ),
            max_timeout=get_value(config.get("max_timeout"), fallback_config.max_timeout if fallback_config else 60),
            http_proxy=get_value(
                config.get("http_proxy"),
                fallback_config.http_proxy if fallback_config else None,
            ),
            https_proxy=get_value(
                config.get("https_proxy"),
                fallback_config.https_proxy if fallback_config else None,
            ),
            no_proxy=get_value(
                config.get("no_proxy"),
                fallback_config.no_proxy if fallback_config else None,
            ),
        )

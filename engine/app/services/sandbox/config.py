"""Sandbox configuration models."""

from collections.abc import Mapping
from enum import StrEnum
from typing import ClassVar, Self, TypedDict

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.core.json_types import json_as_str_or
from app.core.logging import logger
from app.core.outbound_policy import OutboundPolicyError, ensure_same_origin
from app.core.outbound_transport import operator_http_client, public_http_client
from app.services.tool_runtime.provenance import ConfigLayer, ConfigProvenance

ENDPOINT_BLOCKED_MESSAGE = "Sandbox endpoint is blocked by the outbound network policy"


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
    max_stdout_bytes: int
    max_stderr_bytes: int
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


def _same_origin(left: str, right: str) -> bool:
    """Compare endpoint origins; an empty URL means the backend default and only matches itself."""
    if left == right:
        return True
    if not left or not right:
        return False
    try:
        ensure_same_origin(left, right)
    except OutboundPolicyError:
        return False
    return True


def _writer(config: Mapping[str, object], provenance: ConfigProvenance | None, key: str) -> ConfigLayer | None:
    """Layer that supplied a configured value, or ``None`` when the platform default supplies it."""
    if config.get(key) in (None, ""):
        return None
    source = provenance.sources.get(key) if provenance is not None else None
    return source.layer if source is not None else ConfigLayer.UNTRUSTED


def _bound_api_key(
    config: Mapping[str, object], fallback: SandboxConfig | None, provenance: ConfigProvenance | None, api_url: str
) -> str:
    """Return the key only for the origin it was issued for: its own layer's endpoint, else the platform's."""
    fallback_url = fallback.api_url if fallback is not None else ""
    key = json_as_str_or(config.get("api_key"))
    if key:
        source = provenance.sources.get("api_key") if provenance is not None else None
        issued_for = (source.endpoint if source is not None else json_as_str_or(config.get("api_url"))) or fallback_url
    else:
        key, issued_for = (fallback.api_key, fallback_url) if fallback is not None else ("", "")
    if key and not _same_origin(issued_for, api_url):
        logger.warning("[SandboxConfig] Dropped api_key issued for a different endpoint")
        return ""
    return key


def _operator_authorized(
    config: Mapping[str, object], fallback: SandboxConfig | None, provenance: ConfigProvenance | None, api_url: str
) -> bool:
    """Operator authority needs the endpoint and adapter chosen by built-in global or platform settings."""
    url_layer = _writer(config, provenance, "api_url")
    type_layer = _writer(config, provenance, "sandbox_type")
    if not api_url or any(layer not in (None, ConfigLayer.BUILTIN_GLOBAL) for layer in (url_layer, type_layer)):
        return False
    if url_layer is not None:
        return True
    return fallback is not None and fallback.operator_url is not None and _same_origin(fallback.operator_url, api_url)


class SandboxConfig(BaseModel):
    """Configuration for sandbox backend."""

    model_config: ClassVar[ConfigDict] = ConfigDict(use_enum_values=True)

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
    max_stdout_bytes: int = Field(default=1_000_000, ge=0, strict=True)
    max_stderr_bytes: int = Field(default=500_000, ge=0, strict=True)

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

    # Server-established authority to reach a private endpoint. A private attribute cannot be set from JSON,
    # validation input or ``model_validate``; it never appears in dumps.
    _operator_url: str | None = PrivateAttr(default=None)

    @property
    def operator_url(self) -> str | None:
        """Endpoint granted private-network authority by server-owned configuration, if any."""
        return self._operator_url

    def grant_operator_endpoint(self) -> Self:
        """Mark ``api_url`` as operator-owned. Only server-owned construction may call this."""
        self._operator_url = self.api_url or None
        return self

    def http_client(self, *, base_url: str, timeout: float) -> httpx.AsyncClient:
        """Return an origin-bound client; private destinations are allowed only for the operator's origin."""
        if self._operator_url is not None and _same_origin(self._operator_url, base_url):
            return operator_http_client(timeout=timeout, origin=base_url)
        return public_http_client(timeout=timeout, origin=base_url)

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
        if self.http_proxy not in (None, ""):
            env["http_proxy"] = self.http_proxy
            env["HTTP_PROXY"] = self.http_proxy
        if self.https_proxy not in (None, ""):
            env["https_proxy"] = self.https_proxy
            env["HTTPS_PROXY"] = self.https_proxy
        if self.no_proxy not in (None, ""):
            env["no_proxy"] = self.no_proxy
            env["NO_PROXY"] = self.no_proxy
        return env

    @classmethod
    def from_dict(
        cls,
        config: SandboxConfigOverrides,
        fallback_config: SandboxConfig | None = None,
        *,
        provenance: ConfigProvenance | None = None,
    ) -> SandboxConfig:
        """Build SandboxConfig from a dict with field-level fallback support.

        The loader owns decryption, so ``api_key`` arrives as plaintext. A key is kept only for the origin it was
        issued for; no key is substituted when the endpoint changes.

        Args:
            config: Tool configuration dict.
            fallback_config: Fallback configuration, usually from environment variables.
            provenance: Layer facts captured by the loader; without it the dict is treated as untrusted.

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

        api_url = get_value(config.get("api_url"), fallback_config.api_url if fallback_config else "")
        api_key = _bound_api_key(config, fallback_config, provenance, api_url)

        allow_network = get_value(
            config.get("allow_network"),
            fallback_config.allow_network if fallback_config else False,
        )
        logger.info(f"[SandboxConfig] allow_network: raw={config.get('allow_network')!r}, resolved={allow_network!r}")

        resolved = cls(
            type=sandbox_type,
            enabled=True,  # Always enabled when explicitly configured
            api_key=api_key,
            api_url=api_url,
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
            max_stdout_bytes=get_value(
                config.get("max_stdout_bytes"), fallback_config.max_stdout_bytes if fallback_config else 1_000_000
            ),
            max_stderr_bytes=get_value(
                config.get("max_stderr_bytes"), fallback_config.max_stderr_bytes if fallback_config else 500_000
            ),
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
        if _operator_authorized(config, fallback_config, provenance, api_url):
            _ = resolved.grant_operator_endpoint()
        return resolved

"""Runtime helpers for optional gogcli support in OpenClaw containers."""

import re
from pathlib import Path
from typing import ClassVar, Final, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.config import get_settings

settings = get_settings()

GOGCLI_ENABLED = settings.GOGCLI_ENABLED
GOG_HOME = "/home/node/.openclaw/gogcli"
GOG_KEYRING_BACKEND = "file"
GOGCLI_SECRET_MOUNT_PATH = "/run/secrets/gogcli_keyring_password"  # noqa: S105 - path, not a password
_SECRET_ROOT_NAME = "_gogcli_secrets"  # noqa: S105 - directory name, not a secret value
_SECRET_FILE_NAME = "keyring_password"  # noqa: S105 - file name, not a password
_GOOGLE_OAUTH_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https://accounts\.google\.com/o/oauth2/v2/auth\?[^\s\])>'\"]+"
)
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


class GogcliAuthStatus(BaseModel):
    """Safe gogcli authentication status for API/runtime callers."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    authenticated: bool
    account_hint: str | None
    detail: str


class GogcliAgent(Protocol):
    """Agent fields required by gogcli runtime helpers."""

    @property
    def id(self) -> UUID | str: ...

    @property
    def gogcli_enabled(self) -> bool: ...


def _secret_root() -> Path:
    local_root = settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR
    return Path(local_root) / _SECRET_ROOT_NAME


def gogcli_secret_file(agent_id: UUID | str) -> Path:
    """Return the backend-local keyring secret file path for one agent."""
    return _secret_root() / str(agent_id) / _SECRET_FILE_NAME


def extract_gogcli_auth_url(stdout: str) -> str | None:
    """Extract the Google OAuth consent URL from gogcli stdout."""
    match = _GOOGLE_OAUTH_URL_RE.search(stdout)
    if match is None:
        return None
    return match.group(0).rstrip(".,;")


def parse_gogcli_auth_status(stdout: str, stderr: str, exit_code: int) -> GogcliAuthStatus:
    """Parse gogcli auth status into a sanitized status object."""
    del stderr
    account_match = _EMAIL_RE.search(stdout)
    if exit_code == 0 and account_match is not None:
        return GogcliAuthStatus(authenticated=True, account_hint=account_match.group(0), detail="Authenticated")
    return GogcliAuthStatus(authenticated=False, account_hint=None, detail="Not authenticated")


def write_gogcli_keyring_secret(agent_id: UUID | str, password: str) -> Path:
    """Atomically store the per-agent gogcli file-keyring password."""
    secret_file = gogcli_secret_file(agent_id)
    secret_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    secret_file.parent.chmod(0o700)
    tmp_file = secret_file.with_name(f"{_SECRET_FILE_NAME}.tmp")
    _ = tmp_file.write_text(password, encoding="utf-8")
    tmp_file.chmod(0o600)
    _ = tmp_file.replace(secret_file)
    secret_file.chmod(0o600)
    return secret_file


def gogcli_docker_extras(agent: GogcliAgent, agent_dir: Path) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Return extra Docker env and mounts for one gogcli-enabled agent."""
    del agent_dir
    if not GOGCLI_ENABLED or not settings.GOGCLI_ENABLED or not agent.gogcli_enabled:
        return {}, []

    envs = {
        "GOG_HOME": GOG_HOME,
        "GOG_KEYRING_BACKEND": GOG_KEYRING_BACKEND,
    }
    secret_file = gogcli_secret_file(agent.id)
    if not secret_file.exists():
        return envs, []

    envs["GOG_KEYRING_PASSWORD_FILE"] = GOGCLI_SECRET_MOUNT_PATH
    return envs, [(str(secret_file), GOGCLI_SECRET_MOUNT_PATH, "ro")]

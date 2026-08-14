"""OAuth handoff helpers for gogcli inside OpenClaw containers."""

from collections.abc import Iterable, Sequence
from typing import Final, Protocol, ClassVar

from anyio.to_thread import run_sync
from pydantic import BaseModel, ConfigDict
from python_on_whales import ClientNotFoundError
from python_on_whales.exceptions import DockerException, NoSuchContainer

from app.services.gogcli_runtime import GogcliAuthStatus, extract_gogcli_auth_url, parse_gogcli_auth_status

type DockerExecuteOutput = str | Iterable[tuple[str, bytes]] | None

_GOGCLI_SECRET_ENV_SCRIPT: Final = """
set -eu
if [ -n "${GOG_KEYRING_PASSWORD_FILE:-}" ]; then
    if [ ! -f "$GOG_KEYRING_PASSWORD_FILE" ]; then
        printf '%s\n' "GOG_KEYRING_PASSWORD_FILE does not exist: $GOG_KEYRING_PASSWORD_FILE" >&2
        exit 1
    fi
    GOG_KEYRING_PASSWORD=$(cat "$GOG_KEYRING_PASSWORD_FILE")
    export GOG_KEYRING_PASSWORD
fi
exec "$@"
""".strip()
_GOGCLI_EXEC_LABEL: Final = "gogcli-secret-env"


class GogcliDockerClient(Protocol):
    """Docker client surface required for gogcli exec."""

    def execute(self, container_id: str, command: Sequence[str], /, *, stream: bool = False) -> DockerExecuteOutput: ...


class GogcliContainerAgent(Protocol):
    """Agent fields required for gogcli OAuth execution."""

    @property
    def container_id(self) -> str | None: ...


class GogcliAuthStartResult(BaseModel):
    """Safe gogcli OAuth start result for API/runtime callers."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    started: bool
    auth_url: str | None
    detail: str


def _docker_execute_result_parts(output: DockerExecuteOutput) -> tuple[str, str, int]:
    if output is None:
        return "", "", 0
    if isinstance(output, str):
        return output, "", 0

    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    for source, chunk in output:
        if source == "stdout":
            stdout_parts.append(chunk)
        elif source == "stderr":
            stderr_parts.append(chunk)
    return (
        b"".join(stdout_parts).decode("utf-8", errors="replace"),
        b"".join(stderr_parts).decode("utf-8", errors="replace"),
        0,
    )


def _docker_exception_result_parts(error: DockerException) -> tuple[str, str, int]:
    return error.stdout or "", error.stderr or "", error.return_code


def _gogcli_secret_env_command(command: Sequence[str]) -> list[str]:
    return ["sh", "-c", _GOGCLI_SECRET_ENV_SCRIPT, _GOGCLI_EXEC_LABEL, *command]


async def _execute_gogcli_command(
    docker_client: GogcliDockerClient,
    container_id: str,
    command: Sequence[str],
) -> tuple[str, str, int]:
    argv = _gogcli_secret_env_command(command)

    def execute() -> DockerExecuteOutput:
        return docker_client.execute(container_id, argv, stream=False)

    try:
        return _docker_execute_result_parts(await run_sync(execute))
    except DockerException as error:
        return _docker_exception_result_parts(error)


async def start_gogcli_auth(
    docker_client: GogcliDockerClient | None,
    agent: GogcliContainerAgent,
    account_email: str,
) -> GogcliAuthStartResult:
    """Start gogcli OAuth handoff inside the running OpenClaw container."""
    if docker_client is None:
        return GogcliAuthStartResult(started=False, auth_url=None, detail="Agent container is not available")
    if not agent.container_id:
        return GogcliAuthStartResult(started=False, auth_url=None, detail="Agent container is not running")

    command = [
        "gog",
        "auth",
        "add",
        account_email,
        "--services",
        "all-user",
        "--remote",
        "--step",
        "1",
        "--plain",
        "--no-input",
    ]
    try:
        stdout, _, exit_code = await _execute_gogcli_command(docker_client, agent.container_id, command)
    except NoSuchContainer, ClientNotFoundError:
        return GogcliAuthStartResult(started=False, auth_url=None, detail="Agent container is not available")

    auth_url = extract_gogcli_auth_url(stdout)
    if exit_code == 0 and auth_url is not None:
        return GogcliAuthStartResult(started=True, auth_url=auth_url, detail="Authentication started")
    return GogcliAuthStartResult(started=False, auth_url=None, detail="Authentication could not be started")


async def get_gogcli_auth_status(
    docker_client: GogcliDockerClient | None,
    agent: GogcliContainerAgent,
) -> GogcliAuthStatus:
    """Read sanitized gogcli auth status from the running OpenClaw container."""
    if docker_client is None:
        return GogcliAuthStatus(authenticated=False, account_hint=None, detail="Agent container is not available")
    if not agent.container_id:
        return GogcliAuthStatus(authenticated=False, account_hint=None, detail="Agent container is not running")

    command = ["gog", "auth", "list", "--check", "--json", "--no-input"]
    try:
        stdout, stderr, exit_code = await _execute_gogcli_command(docker_client, agent.container_id, command)
    except NoSuchContainer, ClientNotFoundError:
        return GogcliAuthStatus(authenticated=False, account_hint=None, detail="Agent container is not available")
    return parse_gogcli_auth_status(stdout, stderr, exit_code)

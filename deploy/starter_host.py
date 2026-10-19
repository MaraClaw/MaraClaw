import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse

from deploy.autoscale_docker import environment, run


def endpoint() -> str:
    context = os.environ.get("DOCKER_CONTEXT")
    host = os.environ.get("DOCKER_HOST")
    if context or not host:
        host = run(["docker", "context", "inspect", *([context] if context else []),
                    "--format", "{{.Endpoints.docker.Host}}"]).strip()
    parsed = urlparse(host)
    if parsed.scheme != "unix" or parsed.netloc or not parsed.path.startswith("/"):
        raise ValueError("a local Unix Docker endpoint is required")
    return host


def prerequisites(darwin: bool) -> None:
    if sys.version_info < (3, 14):
        raise ValueError("Python >= 3.14 is required")
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("Docker CLI is required")
    os.environ["MARACLAW_DOCKER_BINARY"] = str(Path(docker).absolute())
    host = endpoint()
    if not darwin and host != "unix:///var/run/docker.sock":
        raise ValueError("Linux requires the rootful local Docker socket")
    os.environ["DOCKER_HOST"] = host
    os.environ.pop("DOCKER_CONTEXT", None)
    version = run(["docker", "compose", "version", "--short"]).strip().lstrip("v")
    if tuple(map(int, version.split("-")[0].split(".")[:2])) < (2, 24):
        raise ValueError("Docker Compose >= 2.24 is required")
    if run(["docker", "info", "--format", "{{.OSType}}"]).strip() != "linux":
        raise ValueError("a Linux Docker daemon is required")


def private_directory(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("state must be an absolute private directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    status = path.stat()
    if status.st_uid != os.getuid() or status.st_mode & 0o077:
        raise ValueError("state must be owned by this user with mode 0700")


def execute(command: list[str], timeout: int = 30) -> None:
    executable = os.environ.get("MARACLAW_DOCKER_BINARY", "docker") if command[0] == "docker" else command[0]
    subprocess.run([executable, *command[1:]], env=environment(), check=True, timeout=timeout)

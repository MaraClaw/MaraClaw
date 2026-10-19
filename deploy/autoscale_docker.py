from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import NotRequired, TypedDict

from deploy.autoscale_policy import GIB, POOLS, Policy, admit
from deploy.autoscale_development import validate


class HealthRecord(TypedDict):
    Status: str


class StateRecord(TypedDict):
    Running: bool
    StartedAt: str
    ExitCode: int
    Health: NotRequired[HealthRecord]


class NetworkRecord(TypedDict):
    IPAddress: str


class InspectRecord(TypedDict):
    Id: str
    Image: str
    State: StateRecord
    Config: dict[str, dict[str, str]]
    HostConfig: dict[str, int]
    NetworkSettings: dict[str, dict[str, NetworkRecord]]


class ServiceRecord(TypedDict):
    image: NotRequired[str]
    mem_limit: NotRequired[int]
    deploy: NotRequired[dict[str, int]]
    volumes: NotRequired[list[dict[str, str]]]


class ComposeRecord(TypedDict):
    name: str
    services: dict[str, ServiceRecord]
    networks: dict[str, dict[str, str]]


@dataclass(frozen=True, slots=True)
class Container:
    identity: str
    project: str
    service: str
    running: bool
    healthy: bool
    ip: str
    memory: int
    started: str
    oneoff: bool = False
    exit_code: int = -1
    config_hash: str = ""
    image: str = ""
    development: str = ""


@dataclass(frozen=True, slots=True)
class Service:
    memory: int
    replicas: int


def environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "TMPDIR",
        "NODE_COMPILE_CACHE", "BUN_RUNTIME_TRANSPILER_CACHE_PATH", "MARACLAW_ENV_FILE",
    }}


def run(command: list[str], timeout: int = 30) -> str:
    executable = os.environ.get("MARACLAW_DOCKER_BINARY", "docker") if command[0] == "docker" else command[0]
    result = subprocess.run([executable, *command[1:]], env=environment(), capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("Docker operation failed; inspect Docker locally")
    return result.stdout


class Docker:
    def __init__(self, root: Path, files: tuple[Path, ...], state_dir: Path | None = None):
        self.root = root
        self.startup_reusable = False
        env_file, *compose_files = files
        self.files = tuple(compose_files)
        self.development = None
        for source in self.files:
            if source.suffix == ".json" and source.is_file():
                metadata = validate(source)
                if metadata is not None:
                    if (metadata["files"] != [str(item) for item in self.files] or metadata["state"] != str(state_dir)
                            or set(metadata["inputs"]) != {str(env_file), *(str(item) for item in self.files if item != source)}):
                        raise ValueError("development ordered files or state changed")
                    self.development = metadata
        if state_dir is not None and (state_dir / "development.json").exists() and self.development is None:
            raise ValueError("development effective file omitted")
        self.compose = ["docker", "compose", "--project-directory", str(root), "--env-file",
                        str(env_file), *(word for source in compose_files for word in ("-f", str(source)))]
        model: ComposeRecord = json.loads(run(self.compose + ["config", "--format", "json", "--no-env-resolution"]))
        self.project = model["name"]
        self.network = model["networks"]["app"]["name"]
        self.services = {name: Service(int(service.get("mem_limit", 0)),
                                      service.get("deploy", {}).get("replicas", 1))
                         for name, service in model["services"].items()}
        self.images = {name: service.get("image", "") for name, service in model["services"].items()}
        mounts = model["services"]["ingress"].get("volumes", [])
        self.state_dir = state_dir or Path(next(mount["source"] for mount in mounts
                                   if mount["target"] == "/run/maraclaw-autoscaler"))

    def containers(self) -> tuple[Container, ...]:
        identities = run(["docker", "ps", "-aq", "--no-trunc"]).split()
        if not identities:
            return ()
        records: list[InspectRecord] = json.loads(run(["docker", "inspect", *identities]))
        result = []
        for record in records:
            identity = record["Id"]
            if not re.fullmatch("[0-9a-f]{64}", identity):
                raise ValueError("invalid container identity")
            labels = record["Config"]["Labels"] or {}
            state = record["State"]
            network = record["NetworkSettings"]["Networks"].get(self.network)
            result.append(Container(identity, labels.get("com.docker.compose.project", ""),
                                    labels.get("com.docker.compose.service", ""), state["Running"],
                                    state.get("Health", {"Status": "healthy"}).get("Status") == "healthy",
                                    network["IPAddress"] if network else "", record["HostConfig"]["Memory"],
                                    state["StartedAt"], labels.get("com.docker.compose.oneoff", "False").lower() == "true",
                                     state["ExitCode"], labels.get("com.docker.compose.config-hash", ""), record["Image"],
                                     labels.get("com.maraclaw.development", "")))
        return tuple(result)

    def owned(self, containers: tuple[Container, ...], service: str) -> tuple[Container, ...]:
        return tuple(item for item in containers if item.project == self.project
                     and item.service == service and not item.oneoff)

    def memory(self, containers: tuple[Container, ...], policy: Policy) -> tuple[int, int, int | None]:
        total = int(run(["docker", "info", "--format", "{{.MemTotal}}" ]))
        stack, external, unknown = 0, 0, False
        for item in containers:
            if not item.running:
                continue
            if item.project == self.project and item.service in self.services and not item.oneoff:
                limit = max(item.memory, self.services[item.service].memory)
                if not item.memory or not self.services[item.service].memory:
                    raise ValueError("unlimited project container")
                stack += limit
            elif item.memory:
                external += item.memory
            else:
                unknown = True
        if unknown:
            if policy.external_unlimited_gib <= 0:
                return total, stack, None
            external += int(policy.external_unlimited_gib * GIB)
        return total, stack, external

    def validate_files(self) -> None:
        for source in self.files:
            if source.suffix == ".json":
                metadata = validate(source)
                if metadata is not None and metadata != self.development:
                    raise ValueError("development effective settings changed during operation")

    def grow(self, service: str, count: int) -> None:
        self.validate_files()
        run(self.compose + ["up", "-d", "--no-deps", "--no-recreate", "--no-build", "--pull", "never",
                            "--scale", f"{service}={count}", service], 120)

    def remove(self, identity: str) -> None:
        self.validate_files()
        run(["docker", "stop", "--time", "60", identity], 75)
        self.validate_files()
        run(["docker", "rm", identity])

    def startup_fingerprint(self, containers: tuple[Container, ...]) -> str | None:
        self.validate_files()
        hashes = dict(line.split(maxsplit=1) for line in run(self.compose + ["config", "--hash", "*"]).splitlines())
        images = sorted(set(self.images.values()))
        image_ids = dict(zip(images, run(["docker", "image", "inspect", "--format", "{{.Id}}", *images]).splitlines(), strict=True))
        members = tuple(item for item in containers if item.project == self.project
                        and item.service in self.services and not item.oneoff)
        if any(item.config_hash != hashes[item.service] or item.image != image_ids[self.images[item.service]]
               for item in members):
            return None
        bootstrap_ids = sorted(item.identity for item in members if item.service == "bootstrap")
        digest = hashlib.sha256(json.dumps([hashes, image_ids, bootstrap_ids], sort_keys=True).encode())
        digest.update((self.root / "deploy/bootstrap.py").read_bytes())
        return digest.hexdigest()

    def remember_startup(self) -> None:
        containers = self.containers()
        bootstrap = self.owned(containers, "bootstrap")
        if len(bootstrap) != 1 or bootstrap[0].running or bootstrap[0].exit_code != 0:
            raise ValueError("bootstrap has not completed successfully")
        fingerprint = self.startup_fingerprint(containers)
        if fingerprint is None:
            raise ValueError("deployed containers differ from startup configuration")
        temporary = self.state_dir / "startup.sha256.tmp"
        temporary.write_text(fingerprint)
        temporary.replace(self.state_dir / "startup.sha256")

    def startup(self, policy: Policy) -> dict[str, int]:
        self.validate_files()
        containers = self.containers()
        if self.development is not None and any(item.project == self.project and not item.oneoff
                and item.development != self.development["token"] for item in containers):
            raise ValueError("cannot adopt existing non-development containers")
        if any(item.project == self.project and not item.oneoff and not item.memory for item in containers):
            raise ValueError("unlimited project container")
        total, running_stack, external = self.memory(containers, policy)
        counts = {name: len(self.owned(containers, name)) or self.services[name].replicas for name in POOLS}
        if any(not policy.minimum <= count <= policy.maximum for count in counts.values()):
            raise ValueError("startup replica count outside policy")
        limits = {name: max([spec.memory, *(item.memory for item in self.owned(containers, name))])
                  for name, spec in self.services.items()}
        if any(value <= 0 for value in limits.values()):
            raise ValueError("all services require hard memory limits")
        bootstrap_members = self.owned(containers, "bootstrap")
        complete = (len(bootstrap_members) == 1 and not bootstrap_members[0].running
                    and bootstrap_members[0].exit_code == 0)
        ready = all(len(self.owned(containers, name)) == counts.get(name, spec.replicas)
                    and all(item.running and item.healthy for item in self.owned(containers, name))
                    for name, spec in self.services.items() if name != "bootstrap")
        checkpoint = self.state_dir / "startup.sha256"
        self.startup_reusable = (complete and ready and checkpoint.is_file()
                                 and checkpoint.read_text() == self.startup_fingerprint(containers))
        steady = sum(limits[name] * counts.get(name, spec.replicas)
                     for name, spec in self.services.items() if name != "bootstrap")
        bootstrap = 0 if self.startup_reusable else limits.get("bootstrap", 0)
        cold = sum(limits[name] * counts.get(name, spec.replicas) for name, spec in self.services.items()
                   if name not in {"engine", "background", "bootstrap"}) + bootstrap
        required = max(steady, cold, running_stack + bootstrap)
        if self.development is not None:
            required = max(required, steady + bootstrap)
        if not admit(total, required, external, policy):
            raise ValueError("startup memory admission refused")
        return counts

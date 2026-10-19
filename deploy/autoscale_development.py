import hashlib
import json
import os
from pathlib import Path
import re
from typing import TypedDict


MEMORY = {"engine": ("ENGINE_MEMORY", "768m"), "background": ("BACKGROUND_MEMORY", "768m"),
          "bootstrap": ("BOOTSTRAP_MEMORY", "768m"), "postgres": ("POSTGRES_MEMORY", "768m"),
          "redis": ("REDIS_MEMORY", "128m"), "web-a": ("WEB_MEMORY", "128m"),
          "web-e": ("WEB_MEMORY", "128m"), "ingress": ("INGRESS_MEMORY", "128m")}
REPLICAS = {"engine": "API_REPLICAS", "web-a": "ADMIN_REPLICAS", "web-e": "MEMBER_REPLICAS"}
CPU = {name: ("WEB_CPUS" if name.startswith("web-") else name.upper() + "_CPUS") for name in MEMORY}
RESOURCE_KEYS = {*REPLICAS.values(), *(key for key, _ in MEMORY.values()), *CPU.values()}


class Manifest(TypedDict):
    inputs: dict[str, str]
    files: list[str]
    host: str
    state: str
    token: str
    digest: str


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate(path: Path) -> Manifest | None:
    model = json.loads(path.read_text()) if path.suffix == ".json" else {}
    metadata: Manifest | None = model.get("x-maraclaw-development")
    if metadata is None:
        if path.name == "development.json":
            raise ValueError("development metadata missing")
        return None
    expected = metadata["digest"]
    unsigned = json.loads(json.dumps(model))
    unsigned["x-maraclaw-development"]["digest"] = ""
    if digest(unsigned) != expected:
        raise ValueError("development override changed; use a new isolated project/state")
    if metadata["host"] != os.environ.get("DOCKER_HOST", ""):
        raise ValueError("development Docker endpoint changed")
    for source, fingerprint in metadata["inputs"].items():
        if hashlib.sha256(Path(source).read_bytes()).hexdigest() != fingerprint:
            raise ValueError("development input changed; use a new isolated project/state")
    return metadata


def prepare(files: tuple[Path, ...], state: Path, environment: dict[str, str]) -> Path:
    path = state / "development.json"
    previous = {}
    if path.exists():
        validate(path)
        previous = json.loads(path.read_text())["services"]
    keys = set(re.findall(r"(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=", files[0].read_text()))
    explicit = keys | (RESOURCE_KEYS & os.environ.keys())
    values = environment | {key: os.environ[key] for key in RESOURCE_KEYS if key in os.environ}
    services: dict[str, dict[str, object]] = {}
    token = digest([str(state), os.environ["DOCKER_HOST"], [str(item) for item in files]])
    for name, (key, minimum) in MEMORY.items():
        saved = previous.get(name, {})
        service: dict[str, object] = {"mem_limit": values[key] if key in explicit else saved.get("mem_limit", minimum),
                                     "labels": {"com.maraclaw.development": token}}
        if name in REPLICAS:
            variable = REPLICAS[name]
            service["deploy"] = {"replicas": int(values[variable]) if variable in explicit else saved.get("deploy", {}).get("replicas", 1)}
        if CPU[name] in explicit:
            service["cpus"] = values[CPU[name]]
        elif "cpus" in saved:
            service["cpus"] = saved["cpus"]
        if name == "ingress":
            service["volumes"] = [{"type": "bind", "source": str(state), "target": "/run/maraclaw-autoscaler",
                                   "bind": {"create_host_path": False}}]
        services[name] = service
    metadata: Manifest = {"inputs": {str(item): hashlib.sha256(item.read_bytes()).hexdigest() for item in files},
        "files": [str(item) for item in (files[1], path, *files[2:])], "state": str(state),
        "host": os.environ["DOCKER_HOST"], "token": token, "digest": ""}
    model = {"services": services, "x-maraclaw-development": metadata}
    metadata["digest"] = digest(model)
    if path.exists():
        if json.loads(path.read_text()) != model:
            raise ValueError("effective development settings changed")
    else:
        if (state / "startup.sha256").exists():
            raise ValueError("development override missing from existing state")
        temporary = path.with_suffix(".tmp")
        with temporary.open("x") as output:
            os.chmod(temporary, 0o600)
            json.dump(model, output, sort_keys=True, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    return path

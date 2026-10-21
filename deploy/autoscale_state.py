from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal, TypedDict, assert_never
from urllib.parse import urlparse

from deploy.autoscale_policy import POOLS


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class Binding:
    root: str
    project: str
    state: str
    endpoint: str
    env: str
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Intent:
    identity: str
    ip: str
    service: str
    pool: str
    slot: str
    generation: str

    def validate(self) -> None:
        if (not all(type(value) is str and value for value in asdict(self).values())
                or not re.fullmatch(r"[0-9a-f]{64}", self.identity)
                or self.service not in POOLS or self.pool != POOLS[self.service][0]
                or not re.fullmatch(self.pool.removesuffix("_pool") + r"(?:[1-9]|1[0-6])", self.slot)):
            raise ValueError("invalid retirement identity")


@dataclass(frozen=True, slots=True)
class Operation:
    phase: Literal["draining", "removing", "releasing"]
    victim: Intent


@dataclass(frozen=True, slots=True)
class Authority:
    counts: dict[str, int]
    operation: Operation | None = None


class OperationRecord(TypedDict):
    phase: str
    victim: dict[str, str]


class BindingRecord(TypedDict):
    root: str
    project: str
    state: str
    endpoint: str
    env: str
    files: list[str]


class Record(TypedDict):
    version: int
    binding: BindingRecord
    counts: dict[str, int]
    operation: OperationRecord | None


type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


def unique_fields(pairs: list[tuple[str, Json]]) -> dict[str, Json]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate authority field")
    return result


class ReplicaState:
    def __init__(self, binding: Binding):
        endpoint = urlparse(binding.endpoint)
        if (endpoint.scheme != "unix" or endpoint.netloc or not endpoint.path.startswith("/")
                or endpoint.query or endpoint.fragment or endpoint.params):
            raise ValueError("replica authority requires a local Unix endpoint")
        self.binding = replace(binding, endpoint="unix://" + str(Path(endpoint.path).resolve()))
        self.path = Path(binding.state) / "replicas.json"

    def load(self) -> Authority:
        try:
            record: Record = json.loads(self.path.read_text(), object_pairs_hook=unique_fields)
            expected = json.loads(json.dumps(asdict(self.binding)))
            if (set(record) != {"version", "binding", "counts", "operation"}
                    or type(record["version"]) is not int or record["version"] != 1
                    or record["binding"] != expected):
                raise ValueError("invalid or foreign replica authority")
            counts = record["counts"]
            if (set(counts) != set(POOLS) or any(type(count) is not int or not 1 <= count <= 16
                                              for count in counts.values())):
                raise ValueError("invalid replica targets")
            raw = record["operation"]
            operation = None
            if raw is not None:
                if set(raw) != {"phase", "victim"}:
                    raise ValueError("invalid retirement operation")
                victim = Intent(**raw["victim"])
                victim.validate()
                match raw["phase"]:
                    case "draining" | "removing" | "releasing" as phase:
                        operation = Operation(phase, victim)
                    case _:
                        raise ValueError("invalid retirement phase")
                if operation.phase != "releasing" and counts[victim.service] < 2:
                    raise ValueError("retirement target has no survivor")
            return Authority(counts, operation)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("malformed replica authority") from error

    def save(self, authority: Authority) -> None:
        atomic_write(self.path, json.dumps({"version": 1, "binding": asdict(self.binding),
                     **asdict(authority)}, sort_keys=True))

    def settled(self) -> Authority:
        authority = self.load()
        if authority.operation is not None or (self.path.parent / "pending.json").exists():
            raise ValueError("unresolved retirement evidence")
        return authority

    def reconcile(self, observed: dict[str, int], identities: set[str]) -> Authority:
        authority = self.load()
        operation = authority.operation
        if operation is None:
            return authority
        counts = authority.counts.copy()
        match operation.phase:
            case "releasing":
                return authority
            case "draining":
                pass
            case "removing":
                if operation.victim.identity not in identities:
                    counts[operation.victim.service] -= 1
            case unreachable:
                assert_never(unreachable)
        if any(observed[name] > count for name, count in counts.items()):
            raise ValueError("replica excess during retirement recovery")
        recovered = Authority(counts, replace(operation, phase="releasing"))
        self.save(recovered)
        return recovered

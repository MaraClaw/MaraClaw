import csv
from dataclasses import dataclass
import io
from pathlib import Path
import socket


@dataclass(frozen=True, slots=True)
class Row:
    pool: str
    name: str
    requests: int
    sessions: int
    queued: int
    address: str
    status: str
    connections: tuple[int, ...]

    @property
    def quiet(self) -> bool:
        return self.sessions == self.queued == 0 and all(value == 0 for value in self.connections)


@dataclass(frozen=True, slots=True)
class Snapshot:
    generation: str
    rows: dict[tuple[str, str], Row]
    addresses: dict[tuple[str, str], str]


def number(value: str) -> int:
    result = int(value)
    if result < 0:
        raise ValueError("negative proxy counter")
    return result


def parse_stats(text: str) -> dict[tuple[str, str], Row]:
    if not text.startswith("# pxname,"):
        raise ValueError("missing stats header")
    reader = csv.DictReader(io.StringIO(text[2:]))
    result: dict[tuple[str, str], Row] = {}
    for values in reader:
        pool, name = values["pxname"], values["svname"]
        if not pool.endswith("_pool") or name == "FRONTEND":
            continue
        key = pool, name
        if key in result:
            raise ValueError("duplicate proxy row")
        connections = tuple(number(values[column]) for column in
                            ("idle_conn_cur", "safe_conn_cur", "used_conn_cur", "srv_icur", "priv_idle_cur")
                            if column in values and name != "BACKEND")
        result[key] = Row(pool, name, number(values["req_tot"]) if name == "BACKEND" else 0,
                          number(values["scur"]), number(values["qcur"]),
                          values.get("addr", ""), values["status"], connections)
    return result


def parse_addresses(text: str) -> dict[tuple[str, str], str]:
    lines = text.splitlines()
    if len(lines) < 2 or not lines[1].startswith("# be_id "):
        raise ValueError("missing server state header")
    header = lines[1][2:].split()
    result: dict[tuple[str, str], str] = {}
    for line in lines[2:]:
        if not line.strip():
            continue
        values = dict(zip(header, line.split(), strict=True))
        key = values["be_name"], values["srv_name"]
        if key in result:
            raise ValueError("duplicate server state")
        result[key] = values["srv_addr"]
    return result


class Proxy:
    def __init__(self, path: Path):
        self.path = path

    def command(self, command: str) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(self.path))
            connection.sendall((command + "\n").encode("ascii"))
            connection.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            size = 0
            while chunk := connection.recv(65536):
                size += len(chunk)
                if size > 8 * 1024**2:
                    raise ValueError("oversized proxy response")
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")

    def snapshot(self, ingress: str) -> Snapshot:
        info = dict(line.split(": ", 1) for line in self.command("show info").splitlines() if ": " in line)
        generation = f"{ingress}:{info['Pid']}:{info['Start_time_sec']}"
        return Snapshot(generation, parse_stats(self.command("show stat")),
                        parse_addresses(self.command("show servers state")))

    def state(self, pool: str, slot: str, state: str) -> None:
        response = self.command(f"set server {pool}/{slot} state {state}")
        if response.strip():
            raise ValueError("proxy rejected server state")

    def release(self, pool: str, slot: str) -> None:
        for setting in ("weight 0", "state ready", "health down", "weight 100%"):
            if self.command(f"set server {pool}/{slot} {setting}").strip():
                raise ValueError("proxy rejected slot release")

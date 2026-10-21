import argparse
import contextlib
import http.client
import json
import os
import re
import secrets
import ssl
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import TypedDict

from deploy.autoscale_docker import Docker, environment, run
from deploy.autoscale_policy import GIB, POOLS, Policy, admit
from deploy.autoscale_transport import ExecProxy

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_SERVICES = {"engine", "background", "bootstrap", "web-a", "web-e", "ingress"}


class StoreURLs(TypedDict):
    DATABASE_URL: str
    REDIS_URL: str


def external_stores(path: Path) -> StoreURLs:
    """Read only literal, single-line URLs; never expand shell variables or load operator defaults."""
    values: dict[str, str] = {}
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding="utf-8") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("external env file must be a protected regular file owned by the current user (mode 0600)")
        for line in source.read().split("\n"):
            entry = re.fullmatch(r"\s*(?:export\s+)?(DATABASE_URL|REDIS_URL)\s*=\s*(.*?)\s*", line)
            if entry is None:
                continue
            name, raw = entry.groups()
            if name in values:
                raise ValueError("duplicate external store URL assignment")
            value = re.split(r"\s+#", " " + raw, maxsplit=1)[0].strip()
            if raw[:1] in ("'", '"'):
                quote = raw[0]
                quoted = re.fullmatch(rf"{quote}((?:\\.|[^{quote}\\])*){quote}\s*(?:#.*)?", raw)
                if quoted is None:
                    raise ValueError("external store URLs require closed, single-line dotenv quoting")
                value = quoted[1].replace("\\'", "'") if quote == "'" else re.sub(
                    r'\\([\\"$nrt])', lambda match: {"n": "\n", "r": "\r", "t": "\t"}.get(match[1], match[1]), quoted[1])
            if not value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError("external store URLs must be nonempty single-line values")
            values[name] = value
    if set(values) != {"DATABASE_URL", "REDIS_URL"}:
        raise ValueError("external env file must explicitly supply nonempty DATABASE_URL and REDIS_URL")
    return StoreURLs(DATABASE_URL=values["DATABASE_URL"], REDIS_URL=values["REDIS_URL"])


def request(port: int, host: str, path: str = "/") -> tuple[int, bytes]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with contextlib.closing(http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)) as client:
        client.request("GET", path, headers={"Host": host})
        response = client.getresponse()
        return response.status, response.read()


def authenticate(port: int, password: str) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for supplied, expected in (("incorrect", 401), (password, 200)):
        with contextlib.closing(http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=15)) as client:
            client.request("POST", "/api/auth/login", json.dumps({"login_identifier": "qa@example.test", "password": supplied}),
                           {"Host": "engine.local", "Content-Type": "application/json"})
            response = client.getresponse()
            body = response.read()
            assert response.status == expected, ("auth", response.status)
            if expected == 200:
                assert json.loads(body)["access_token"]


def inspect_health(docker: Docker) -> list[dict[str, object]]:
    identities = [item.identity for item in docker.containers() if item.project == docker.project]
    records = json.loads(run(["docker", "inspect", *identities]))
    result = []
    for item in records:
        service = item["Config"]["Labels"]["com.docker.compose.service"]
        assert not item["State"]["OOMKilled"] and item["RestartCount"] == 0, service
        if service == "bootstrap":
            assert not item["State"]["Running"] and item["State"]["ExitCode"] == 0
        else:
            assert item["State"]["Health"]["Status"] == "healthy", service
        result.append({"service": service, "id": item["Id"], "memory": item["HostConfig"]["Memory"],
                       "oom": item["State"]["OOMKilled"], "restarts": item["RestartCount"]})
    return result


def independent_growth(docker: Docker, proxy: ExecProxy, port: int) -> list[str]:
    from deploy.autoscale_controller import Controller
    policy = Policy(sample_seconds=1, high_samples=2, low_samples=3, cooldown_seconds=2,
                    drain_seconds=30, maximum=2, engine_rps=.2, web_rps=.2)
    events = []
    controller = Controller(docker, proxy, policy, docker.state_dir)
    domains = {"engine": "engine.local", "web-a": "admin.local", "web-e": "member.local"}
    try:
        for service, host in domains.items():
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                for _ in range(4):
                    assert request(port, host, "/api/ready" if service == "engine" else "/")[0] == 200
                event = controller.tick(time.monotonic())
                if event.startswith("grow"):
                    events.append(event)
                    assert event.startswith(f"grow service={service} "), event
                    break
                time.sleep(1)
            else:
                raise AssertionError(f"no independent growth for {service}")
            deadline = time.monotonic() + 120
            while not all(item.running and item.healthy for item in docker.owned(docker.containers(), service)):
                if time.monotonic() > deadline:
                    raise AssertionError(f"grown {service} did not become healthy")
                time.sleep(1)
            inspect_health(docker)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                event = controller.tick(time.monotonic())
                if event == "removed":
                    break
                time.sleep(1)
            else:
                raise AssertionError(f"no safe idle retirement for {service}")
            counts = {name: len(docker.owned(docker.containers(), name)) for name in POOLS}
            assert counts == dict.fromkeys(POOLS, 1), counts
            print(f"PASS independent {service} 1 -> 2 -> 1", flush=True)
    finally:
        controller.retirement.cancel()
    return events


def exercise(directory: Path, stores: StoreURLs) -> dict[str, object]:
    project = "maraclaw-dev-qa-" + uuid.uuid4().hex[:10]
    state = directory / "state"
    data = directory / "data"
    data.mkdir()
    key, certificate, pem = (directory / name for name in ("key.pem", "cert.pem", "tls.pem"))
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=engine.local", "-keyout", str(key), "-out", str(certificate)],
                   env=environment(), capture_output=True, check=True, timeout=30)
    pem.write_bytes(key.read_bytes() + certificate.read_bytes())
    password = secrets.token_hex(20)
    values = {"SECRET_KEY": secrets.token_hex(32), "JWT_SECRET_KEY": secrets.token_hex(32),
        **stores,
        "PLATFORM_ADMIN_EMAIL": "qa@example.test", "PLATFORM_ADMIN_PASSWORD": password,
        "API_DOMAIN": "engine.local", "ADMIN_DOMAIN": "admin.local", "MEMBER_DOMAIN": "member.local",
        "DATA_DIR": str(data), "TLS_PEM_PATH": str(pem), "HTTP_PORT": "127.0.0.1:", "HTTPS_PORT": "127.0.0.1:",
        "DOCKER_NETWORK": project + "_guest"}
    env_file = directory / "synthetic.env"
    env_file.touch(mode=0o600)
    env_file.write_text("\n".join(f"{key}=" + json.dumps(value, ensure_ascii=False).replace("$", "$$") for key, value in values.items()))
    os.environ["MARACLAW_ENV_FILE"] = str(env_file)
    override = directory / "isolated.yaml"
    override.write_text(f"name: {project}\nservices:\n" + "".join(
        f"  {name}:\n    env_file: !reset []\n" for name in ("engine", "background", "bootstrap")))
    arguments = [str(ROOT / "start-docker-compose.sh"), "--development", "--no-build", "--no-install",
                 "--env-file", str(env_file), "--compose-file", str(override), "--state-dir", str(state)]
    cleanup = ["docker", "compose", "--project-name", project, "--project-directory", str(ROOT), "--env-file", str(env_file),
               "-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "deploy/compose.macos.yaml"), "-f", str(override)]
    model = json.loads(run(cleanup + ["config", "--format", "json", "--no-env-resolution"]))
    if set(model["services"]) != APPLICATION_SERVICES:
        raise ValueError("acceptance requires application-only Compose services; external stores must not be provisioned")
    process: subprocess.Popen[str] | None = None
    try:
        with (directory / "startup.log").open("w+") as output:
            process = subprocess.Popen([*arguments, "--foreground"], env=environment(), stdout=output, stderr=output, text=True)
            deadline = time.monotonic() + 300
            while not (state / "startup.sha256").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("foreground startup failed")
                time.sleep(1)
            os.environ["MARACLAW_ENV_FILE"] = str(env_file)
            files = (env_file, ROOT / "compose.yaml", state / "development.json",
                     ROOT / "deploy/compose.macos.yaml", override)
            docker = Docker(ROOT, files, state)
            model = json.loads(run(docker.compose + ["config", "--format", "json", "--no-env-resolution"]))
            assert all(not model["services"][name].get("env_file") for name in ("engine", "background", "bootstrap"))
            port = int(run(docker.compose + ["port", "ingress", "8443"]).strip().rsplit(":", 1)[1])
            for host, path in (("engine.local", "/api/ready"), ("admin.local", "/"), ("member.local", "/")):
                assert request(port, host, path)[0] == 200, host
            authenticate(port, password)
            proxy = ExecProxy(docker)
            ingress = docker.owned(docker.containers(), "ingress")[0]
            snapshot = proxy.snapshot(ingress.identity + ":" + ingress.started)
            assert all((pool, "BACKEND") in snapshot.rows for pool, _ in POOLS.values())
            assert process.poll() is None
            process.terminate()
            assert process.wait(timeout=180) == 0
            before = (state / "development.json").read_bytes(), (state / "startup.sha256").read_bytes()
            identities = {item.identity for item in docker.containers() if item.project == project}
            subprocess.run(arguments, env=environment(), stdout=output, stderr=output, check=True, timeout=300)
            assert before == ((state / "development.json").read_bytes(), (state / "startup.sha256").read_bytes())
            assert identities == {item.identity for item in docker.containers() if item.project == project}
            records = inspect_health(docker)
            print("PASS HTTPS readiness/frontends, bad and valid genesis login, foreground exec transport, unchanged rerun", flush=True)
            events = independent_growth(docker, proxy, port)
            records = inspect_health(docker)
            total, stack, external = docker.memory(docker.containers(), Policy())
            assert admit(total, stack, external, Policy())
            return {"project": project, "daemon_gib": total / GIB, "steady_gib": stack / GIB,
                    "external_bytes": external, "containers": records, "growth": events,
                    "checkpoint_reused": True, "https_and_auth": True, "env_file_removed": True,
                    "foreground_exit": process.returncode}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=180)
        run(cleanup + ["down", "--volumes", "--timeout", "60"], 120)


def main() -> None:
    parser = argparse.ArgumentParser(description="Real macOS qualification against caller-owned disposable external stores; no store provisioning")
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--external-env-file", type=Path, required=True, help="protected dotenv file with literal DATABASE_URL and REDIS_URL; no expansion")
    parser.add_argument("--confirm-disposable-stores", action="store_true", required=True,
                        help="confirm a dedicated disposable PostgreSQL database AND Redis instance: bootstrap/auth/application mutate both")
    parser.add_argument("--receipt", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        stores = external_stores(arguments.external_env_file)
    except (OSError, UnicodeError, ValueError):
        parser.error("supply a readable, current-user-owned mode-0600 file with nonempty, literal single-line DATABASE_URL and REDIS_URL")
    if os.uname().sysname != "Darwin":
        raise ValueError("this qualification exercises the macOS launcher")
    from deploy.starter_host import prerequisites
    prerequisites(True)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="maraclaw-development-qa-") as temporary:
        receipt = exercise(Path(temporary).resolve(), stores)
    receipt["cleanup"] = "uniquely owned application Compose resources, controller and temporary files removed"
    receipt["external_stores"] = {"ownership": "caller", "preserved": True, "disposable_acknowledged": True}
    arguments.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()

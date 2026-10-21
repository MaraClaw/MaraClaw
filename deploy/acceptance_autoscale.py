import argparse
from collections.abc import Mapping
import contextlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import MappingProxyType
import uuid

from deploy.acceptance_socket import start
from deploy.autoscale_docker import Docker, run
from deploy.autoscale_policy import POOLS, Policy
from deploy.autoscale_proxy import Proxy

ROOT = Path(__file__).resolve().parents[1]


def resolve_domains(environment: Mapping[str, str]) -> Mapping[str, str]:
    domains: dict[str, str] = {}
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    for service, (variable, default) in {
        "engine": ("API_DOMAIN", "engine.local"), "web-a": ("ADMIN_DOMAIN", "admin.local"),
        "web-e": ("MEMBER_DOMAIN", "member.local"),
    }.items():
        host = environment.get(variable, default)
        if len(host) > 253 or not re.fullmatch(rf"{label}(?:\.{label})*", host):
            raise ValueError(f"{variable} must be a bare DNS hostname")
        domains[service] = host.lower()
    if len(set(domains.values())) != len(domains):
        raise ValueError("fixture domain hostnames must be distinct")
    return MappingProxyType(domains)


def request(port: int, host: str) -> str:
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as client:
        client.request("GET", "/", headers={"Host": host})
        response = client.getresponse()
        body = response.read().decode()
        if response.status != 200:
            raise RuntimeError("acceptance HTTP request failed")
        return body


def prepare(directory: Path, domains: Mapping[str, str]) -> Docker:
    project = "maraclaw-accept-" + uuid.uuid4().hex[:10]
    source = ROOT / "deploy"
    config = (source / "haproxy.cfg").read_text()
    config = config.split("frontend public_http\n")[0] + """
frontend test_http
    bind :8080
    http-request return status 200 if { path /healthz }
""" + "".join(f"    use_backend {POOLS[service][0]} if {{ hdr(host) -i {host} }}\n"
              for service, host in domains.items()) + """
    default_backend engine_pool
""" + "\nbackend engine_pool\n" + config.split("\nbackend engine_pool\n")[1]
    config = config.replace("engine:8000", "engine:8080").replace("inter 5s", "inter 1s")
    (directory / "haproxy.cfg").write_text(config)
    backend = {"image": "maraclaw-engine:local", "entrypoint": ["python", "/backend.py"],
               "mem_limit": "128m", "networks": ["app"], "stop_grace_period": "60s",
               "volumes": [f"{source / 'acceptance_backend.py'}:/backend.py:ro"],
               "healthcheck": {"test": ["CMD", "python", "-c",
                   "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).read()"],
                   "interval": "1s", "timeout": "3s", "retries": 10}}
    services = {name: backend for name in POOLS}
    services["ingress"] = {
        "image": "haproxy:3.4.6-alpine", "user": "0:0", "mem_limit": "128m", "networks": ["app"],
        "volumes": [f"{directory / 'haproxy.cfg'}:/usr/local/etc/haproxy/haproxy.cfg:ro",
                    "socket:/run/maraclaw-autoscaler"], "ports": ["127.0.0.1::8080"],
        "healthcheck": {"test": ["CMD", "wget", "-qO-", "http://127.0.0.1:8080/healthz"],
                        "interval": "1s", "timeout": "3s", "retries": 10}}
    services["helper"] = {"image": "maraclaw-engine:local", "mem_limit": "128m", "networks": ["app"],
                          "entrypoint": ["python", "-c", "import time;time.sleep(600)"],
                          "healthcheck": {"disable": True},
                          "volumes": ["socket:/run/maraclaw-autoscaler"]}
    model = {"name": project, "services": services, "networks": {"app": {}}, "volumes": {"socket": {}}}
    (directory / "compose.json").write_text(json.dumps(model))
    (directory / "empty.env").touch()
    (directory / "policy.toml").write_text("sample_seconds=1\nhigh_samples=2\nlow_samples=6\n"
        "cooldown_seconds=2\ndrain_seconds=15\nmaximum=4\nengine_rps=2\nweb_rps=2\n")
    state = directory / "controller"
    state.mkdir(mode=0o700)
    return Docker(directory, (directory / "empty.env", directory / "compose.json"), state)


def semantics(proxy: Proxy, docker: Docker, port: int, host: str) -> None:
    containers = docker.owned(docker.containers(), "engine")
    candidate = containers[-1]
    snapshot = proxy.snapshot("acceptance")
    slot = next(name for (pool, name), address in snapshot.addresses.items()
                if pool == "engine_pool" and address == candidate.ip)
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as client:
        for _ in range(20):
            client.request("GET", "/", headers={"Host": host})
            response = client.getresponse()
            if response.read().decode() == candidate.identity[:12]:
                break
        else:
            raise AssertionError("candidate was never routed")
        with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as stream:
            for _ in range(20):
                stream.request("GET", "/stream", headers={"Host": host})
                response = stream.getresponse()
                if response.getheader("X-Backend") == candidate.identity[:12]:
                    break
                stream.close()
            else:
                raise AssertionError("candidate stream was never routed")
            proxy.state("engine_pool", slot, "maint")
            try:
                assert response.read(7) == b"stream\n"
                for _ in range(5):
                    client.request("GET", "/", headers={"Host": host})
                    assert client.getresponse().read().decode() != candidate.identity[:12]
                assert proxy.snapshot("acceptance").rows[("engine_pool", slot)].sessions > 0
                assert response.read(7) == b"stream\n"
            finally:
                proxy.state("engine_pool", slot, "ready")
    print("PASS maint excludes new HTTP on existing client keepalive; active stream survives")


def exercise(directory: Path, docker: Docker, domains: Mapping[str, str]) -> None:
    helper = run(docker.compose + ["ps", "-q", "helper"]).strip()
    controller_dir = docker.state_dir
    relay, worker = start(controller_dir / "admin.sock", helper)
    port = int(run(docker.compose + ["port", "ingress", "8080"]).strip().rsplit(":", 1)[1])
    command = [sys.executable, "-m", "deploy.autoscale", "--root", str(directory),
               "--compose-file", str(directory / "compose.json"), "--env-file", str(directory / "empty.env"),
               "--policy", str(directory / "policy.toml"), "--state-dir", str(controller_dir)]
    controller: subprocess.Popen[str] | None = None
    (directory / "controller.log").touch()
    try:
        Proxy(controller_dir / "admin.sock").snapshot("preflight")
        with (directory / "controller.log").open("w+") as output:
            environment = dict(os.environ, PYTHONPATH=str(ROOT))
            controller = subprocess.Popen(command, env=environment, stdout=output, stderr=output, text=True)
            seen: dict[str, set[str]] = {host: set() for host in domains.values()}
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                containers = [line.split() for line in run(["docker", "ps", "--filter",
                    f"label=com.docker.compose.project={docker.project}", "--format",
                    '{{.Label "com.docker.compose.service"}} {{.ID}}']).splitlines()]
                current = {host: {identity for service, identity in containers if service == name}
                           for name, host in domains.items()}
                counts = [len(identities) for identities in current.values()]
                for host, identities in seen.items():
                    routed = {request(port, host) for _ in range(4)}
                    if counts == [4, 4, 4]:
                        identities.update(routed)
                    else:
                        identities.clear()
                if counts == [4, 4, 4] and all(current[host] <= identities for host, identities in seen.items()):
                    print(f"PASS first peak: project={docker.project}; counts={counts}; routed current backend identities={seen}")
                    break
                if controller.poll() is not None:
                    raise RuntimeError("controller exited during traffic")
            else:
                raise AssertionError("traffic did not scale all pools to four routed replicas")
            controller.terminate()
            controller.wait(timeout=180)
            time.sleep(3)
            semantics(Proxy(controller_dir / "admin.sock"), docker, port, domains["engine"])
            controller = subprocess.Popen(command, env=environment, stdout=output, stderr=output, text=True)
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                services = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={docker.project}",
                                "--format", '{{.Label "com.docker.compose.service"}}']).split()
                counts = [services.count(name) for name in POOLS]
                if counts == [1, 1, 1]:
                    break
                if controller.poll() is not None:
                    raise RuntimeError("controller exited during idle")
                time.sleep(1)
            else:
                raise AssertionError("idle did not scale all pools to one")
            for host in domains.values():
                request(port, host)
            print("PASS actual controller CLI: traffic 1/1/1 -> 4/4/4; idle -> 1/1/1; HTTP remains ready")
            seen = {host: set() for host in seen}
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                containers = [line.split() for line in run(["docker", "ps", "--filter",
                    f"label=com.docker.compose.project={docker.project}", "--format",
                    '{{.Label "com.docker.compose.service"}} {{.ID}}']).splitlines()]
                current = {host: {identity for service, identity in containers if service == name}
                           for name, host in domains.items()}
                counts = [len(identities) for identities in current.values()]
                for host, identities in seen.items():
                    routed = {request(port, host) for _ in range(4)}
                    if counts == [4, 4, 4]:
                        identities.update(routed)
                    else:
                        identities.clear()
                if counts == [4, 4, 4] and all(current[host] <= identities for host, identities in seen.items()):
                    print(f"PASS second peak: project={docker.project}; counts={counts}; routed current backend identities={seen}")
                    break
                if controller.poll() is not None:
                    raise RuntimeError("controller exited during slot reuse")
            else:
                raise AssertionError("reused slots did not route all four current replicas")
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                services = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={docker.project}",
                                "--format", '{{.Label "com.docker.compose.service"}}']).split()
                counts = [services.count(name) for name in POOLS]
                if counts == [1, 1, 1]:
                    break
                if controller.poll() is not None:
                    raise RuntimeError("controller exited during second idle")
                time.sleep(1)
            else:
                raise AssertionError("second idle did not scale all pools to one")
            for host in seen:
                request(port, host)
            print("PASS second traffic/idle cycle: 1/1/1 -> 4/4/4 -> 1/1/1; reused slots retire safely")
    except (TimeoutError, RuntimeError, AssertionError):
        snapshot = Proxy(controller_dir / "admin.sock").snapshot("failure")
        print("Owned fixture proxy failure state:", [row for row in snapshot.rows.values()
              if row.name == "BACKEND" or row.address.rsplit(":", 1)[0] not in {"", "-"}])
        print("Owned fixture containers:", [item for item in docker.containers() if item.project == docker.project])
        raise
    finally:
        if controller is not None and controller.poll() is None:
            controller.terminate()
            controller.wait(timeout=180)
        relay.shutdown()
        relay.server_close()
        worker.join(timeout=5)
        print((directory / "controller.log").read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description="Opt-in isolated Docker acceptance; cached images only")
    parser.add_argument("--run", action="store_true", required=True)
    parser.parse_args()
    domains = resolve_domains(os.environ)
    for image in ("maraclaw-engine:local", "haproxy:3.4.6-alpine"):
        run(["docker", "image", "inspect", image])
    with tempfile.TemporaryDirectory(prefix="maraclaw-autoscale-accept-") as temporary:
        directory = Path(temporary)
        os.environ.update(TMPDIR=temporary, NODE_COMPILE_CACHE=str(directory / "node"),
                          BUN_RUNTIME_TRANSPILER_CACHE_PATH=str(directory / "bun"))
        docker = prepare(directory, domains)
        os.chdir(directory)
        try:
            docker.startup(Policy.load(directory / "policy.toml"))
            result = subprocess.run(docker.compose + ["up", "-d", "--no-build", "--pull", "never", "--wait",
                "--wait-timeout", "60"], capture_output=True, text=True, timeout=90, check=False)
            if result.returncode:
                print(result.stderr)
                print(run(docker.compose + ["logs", "--tail", "20"]))
                raise RuntimeError("isolated acceptance startup failed")
            exercise(directory, docker, domains)
        finally:
            run(docker.compose + ["down", "--volumes", "--timeout", "60"], 90)
            os.chdir(ROOT)


if __name__ == "__main__":
    main()

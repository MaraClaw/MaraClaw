from collections.abc import Mapping
import contextlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from deploy import acceptance_autoscale as acceptance
from deploy.autoscale_docker import Container, Docker
from deploy.autoscale_policy import POOLS
from deploy.autoscale_proxy import Proxy, Row, Snapshot


DEFAULTS = {"engine": "engine.local", "web-a": "admin.local", "web-e": "member.local"}
OVERRIDES = {"API_DOMAIN": "api.fixture.invalid", "ADMIN_DOMAIN": "ops.fixture.invalid",
             "MEMBER_DOMAIN": "people.fixture.invalid"}
CUSTOM = dict(zip(POOLS, OVERRIDES.values(), strict=True))


class FixtureHTTP(ThreadingHTTPServer):
    def __init__(self, routes: dict[str, str]):
        self.routes = routes
        self.epoch = 0
        self.maint = False
        self.counters: dict[tuple[str, str], int] = {}
        self.events: list[tuple[int, str, str, bool]] = []
        super().__init__(("127.0.0.1", 0), Handler)

    @staticmethod
    def identity(service: str, epoch: int, index: int) -> str:
        return f"{service}-{epoch}-{index}".ljust(12, "0")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, FixtureHTTP)
        host = self.headers.get("Host", "")
        server.events.append((server.epoch, host, self.path, server.maint))
        if host not in server.routes:
            self.send_error(404)
            return
        service = server.routes[host]
        key = host, self.path
        index = server.counters.get(key, 0) % (3 if server.maint and service == "engine" else 4)
        server.counters[key] = index + 1
        identity = server.identity(service, server.epoch, index)
        body = b"stream\nstream\n" if self.path == "/stream" else identity.encode()
        self.send_response(200)
        self.send_header("X-Backend", identity)
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/stream":
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class DomainTests(unittest.TestCase):
    def test_defaults_when_environment_is_unset(self) -> None:
        self.assertEqual(acceptance.resolve_domains({}), DEFAULTS)

    def test_overrides_when_environment_is_set(self) -> None:
        self.assertEqual(acceptance.resolve_domains(OVERRIDES), CUSTOM)

    def test_unset_domains_keep_defaults_when_one_is_overridden(self) -> None:
        self.assertEqual(acceptance.resolve_domains({"API_DOMAIN": "API.Example.Test"}),
                         dict(DEFAULTS, engine="api.example.test"))

    def test_invalid_hostnames_are_rejected(self) -> None:
        invalid = ("", " api.test", "api.test ", "api.test\nbackend injected", "api.test\r\n",
                   "api.test }", "api.test#comment", "${API_DOMAIN}", 'api".test', "api\\.test",
                   "https://api.test", "api.test:443", "api.test/path", "api..test", "api.test.",
                   "-api.test", "api-.test", "api_test", "é.test", "a" * 64 + ".test",
                   ".".join(["a" * 63] * 4))
        for variable in OVERRIDES:
            for value in invalid:
                with self.subTest(variable=variable, value=value), self.assertRaises(ValueError):
                    acceptance.resolve_domains(dict(OVERRIDES, **{variable: value}))

    def test_duplicates_are_rejected_case_insensitively(self) -> None:
        for values in ({"API_DOMAIN": "ADMIN.LOCAL"},
                       dict(OVERRIDES, ADMIN_DOMAIN="API.FIXTURE.INVALID")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                acceptance.resolve_domains(values)

    def test_invalid_environment_is_rejected_before_docker(self) -> None:
        with patch.dict(os.environ, {"API_DOMAIN": "bad\nhost"}), \
                patch("sys.argv", ["acceptance_autoscale", "--run"]), \
                patch.object(acceptance, "run") as run, self.assertRaises(ValueError):
            acceptance.main()
        run.assert_not_called()

    def test_main_resolves_once_and_shares_mapping(self) -> None:
        captured: list[Mapping[str, str]] = []

        def prepare(directory: Path, domains: Mapping[str, str]) -> Mock:
            captured.append(domains)
            (directory / "policy.toml").write_text("")
            os.environ.update({key: "changed.invalid" for key in OVERRIDES})
            return Mock(compose=["fixture-compose"])

        with patch.dict(os.environ, OVERRIDES), patch("sys.argv", ["acceptance_autoscale", "--run"]), \
                patch.object(acceptance, "prepare", side_effect=prepare), \
                patch.object(acceptance, "exercise") as exercise, \
                patch.object(acceptance, "run"), patch.object(acceptance.os, "chdir"), \
                patch.object(acceptance.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            acceptance.main()
        self.assertIs(exercise.call_args.args[2], captured[0])
        self.assertEqual(captured[0], CUSTOM)


class TrafficTests(unittest.TestCase):
    def test_generated_routing_and_all_request_paths_share_resolved_domains(self) -> None:
        for environment, expected in (({}, DEFAULTS), (OVERRIDES, CUSTOM)):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                domains = acceptance.resolve_domains(environment)
                docker = Mock(spec=Docker, project="fixture", compose=["fixture-compose"], state_dir=directory / "controller")
                with patch.object(acceptance, "Docker", return_value=docker):
                    acceptance.prepare(directory, domains)
                config = (directory / "haproxy.cfg").read_text()
                service_by_pool = {pool: service for service, (pool, _) in POOLS.items()}
                routes = {host: service_by_pool[pool] for pool, host in re.findall(
                    r"use_backend (\w+) if \{ hdr\(host\) -i ([\w.-]+) \}", config)}
                self.assertEqual(routes, {host: service for service, host in expected.items()})
                model = json.loads((directory / "compose.json").read_text())
                self.assertEqual(model["services"]["ingress"]["ports"], ["127.0.0.1::8080"])
                server = FixtureHTTP(routes)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    self.exercise_fixture(directory, docker, domains, server)
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=5)
                self.assertEqual(server.epoch, 2)
                self.assertEqual({host for _, host, _, _ in server.events}, set(expected.values()))
                self.assertEqual({host for _, host, path, maint in server.events
                                  if path == "/stream" or maint}, {expected["engine"]})
                for epoch in (1, 2):
                    self.assertEqual({host for phase, host, _, _ in server.events if phase == epoch},
                                     set(expected.values()))

    def exercise_fixture(self, directory: Path, docker: Mock,
                         domains: Mapping[str, str], server: FixtureHTTP) -> None:
        candidate = Container(server.identity("engine", 0, 3), "fixture", "engine", True, True,
                              "10.0.0.4", 128, "start")
        docker.containers.return_value = (candidate,)
        docker.owned.return_value = (candidate,)
        row = Row("engine_pool", "engine4", 0, 1, 0, candidate.ip, "UP", ())
        proxy = Mock(spec=Proxy)
        proxy.snapshot.return_value = Snapshot("fixture", {("engine_pool", "engine4"): row},
                                                {("engine_pool", "engine4"): candidate.ip})
        proxy.state.side_effect = lambda pool, slot, state: setattr(server, "maint", state == "maint")

        def run(command: list[str]) -> str:
            if command[-1] == "helper":
                return "helper"
            if "port" in command:
                return f"127.0.0.1:{server.server_port}"
            if "-a" in command:
                server.epoch += 1
                return "\n".join(POOLS)
            return "\n".join(f"{service} {server.identity(service, server.epoch, index)}"
                             for service in POOLS for index in range(4))

        def connection(host: str, port: int, timeout: int) -> HTTPConnection:
            self.assertEqual((host, port), ("127.0.0.1", server.server_port))
            return HTTPConnection(host, port, timeout=timeout)

        clock = Mock()
        clock.monotonic.side_effect = itertools.count(step=50)
        process = Mock()
        process.poll.return_value = None
        output = io.StringIO()
        with contextlib.chdir(directory), contextlib.redirect_stdout(output), \
                patch.dict(os.environ, {key: "changed.invalid" for key in OVERRIDES}), \
                patch.object(acceptance, "run", side_effect=run), \
                patch.object(acceptance, "start", return_value=(Mock(), Mock())), \
                patch.object(acceptance, "Proxy", return_value=proxy), \
                patch.object(acceptance, "time", clock), \
                patch.object(acceptance.subprocess, "Popen", return_value=process), \
                patch.object(acceptance.http.client, "HTTPConnection", side_effect=connection):
            acceptance.exercise(directory, docker, domains)
        self.assertIn("PASS second peak:", output.getvalue())
        self.assertEqual([call.args[2] for call in proxy.state.call_args_list], ["maint", "ready"])


if __name__ == "__main__":
    unittest.main()

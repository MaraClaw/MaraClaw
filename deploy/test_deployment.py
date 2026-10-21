"""Compose contract checks; no containers or application dependencies are started."""

import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "SECRET_KEY", "JWT_SECRET_KEY", "DATABASE_URL", "REDIS_URL", "PLATFORM_ADMIN_EMAIL",
    "PLATFORM_ADMIN_PASSWORD", "API_DOMAIN", "ADMIN_DOMAIN", "MEMBER_DOMAIN",
    "DATA_DIR", "TLS_PEM_PATH",
)


def render(values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="maraclaw-compose-test-") as directory:
        env_file = Path(directory) / ".env"
        env_file.write_text("\n".join(f"{key}='{value}'" for key, value in values.items()))
        environment = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "TMPDIR",
        }}
        return subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml"),
             "config", "--format", "json", "--no-env-resolution"],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30, check=False,
        )


class ComposeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.values = {
            "SECRET_KEY": "unit-secret", "JWT_SECRET_KEY": "different-unit-secret",
            "DATABASE_URL": "postgresql://team%40operator:p%40%20ss%3A%2F%23%25%24@pg.remote.example.test:6432/team_archive?sslmode=verify-full&sslrootcert=%2Frun%2Ftrust%2Fpostgres.pem",
            "REDIS_URL": "rediss://cache%3Aoperator:r%40%20ss%3A%2F%23%25%24@cache.remote.example.test:6380/11?ssl_cert_reqs=required&ssl_ca_certs=%2Frun%2Ftrust%2Fredis.pem",
            "PLATFORM_ADMIN_EMAIL": "admin@example.test", "PLATFORM_ADMIN_PASSWORD": "initial-password",
            "API_DOMAIN": "api.example.test", "ADMIN_DOMAIN": "admin.example.test",
            "MEMBER_DOMAIN": "members.example.test", "DATA_DIR": "/srv/test-maraclaw",
            "TLS_PEM_PATH": "/srv/test-tls.pem",
        }
        result = render(cls.values)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.model = json.loads(result.stdout)
        cls.services = cls.model["services"]

    def test_required_inputs_refused_when_missing_or_empty(self) -> None:
        # Given mandatory inputs; when omitted or empty; then render must fail.
        for key in REQUIRED:
            for value in (None, ""):
                with self.subTest(key=key, value=value):
                    values = dict(self.values)
                    if value is None:
                        del values[key]
                    else:
                        values[key] = value
                    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://shell/wrong", "REDIS_URL": "redis://shell/3"}):
                        result = render(values)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(key, result.stderr)

    def test_consumers_wait_only_for_completed_bootstrap(self) -> None:
        for name in ("engine", "background"):
            with self.subTest(service=name):
                dependencies = self.services[name]["depends_on"]
                self.assertEqual(dependencies["bootstrap"]["condition"], "service_completed_successfully")
                self.assertEqual(set(dependencies), {"bootstrap"})
        self.assertFalse(self.services["bootstrap"].get("depends_on"))
        self.assertEqual(self.services["bootstrap"]["restart"], "no")

    def test_roles_prevent_duplicate_seeds_and_background_loops(self) -> None:
        for name, role, replicas in (("engine", "api", 2), ("background", "worker,connector", 1)):
            with self.subTest(service=name):
                service = self.services[name]
                self.assertEqual(service["environment"]["PROCESS_ROLE"], role)
                self.assertEqual(service["deploy"]["replicas"], replicas)
                self.assertEqual(service["environment"]["INSTANCE_ID"], "")
                self.assertEqual(service["environment"]["PLATFORM_ADMIN_PASSWORD"], "")
                self.assertEqual(service["environment"]["PLATFORM_ADMIN_EMAIL"], "")
                self.assertIn("app.main:app", service["environment"]["START_COMMAND"])
        bootstrap = self.services["bootstrap"]["environment"]
        self.assertEqual(bootstrap["PROCESS_ROLE"], "bootstrap")
        self.assertEqual(bootstrap["START_COMMAND"], "exec python /deployment/bootstrap.py")
        self.assertEqual(bootstrap["PYTHONPATH"], "/app")
        self.assertEqual(bootstrap["PLATFORM_ADMIN_PASSWORD"], self.values["PLATFORM_ADMIN_PASSWORD"])

    def test_inherited_identity_and_genesis_inputs_cannot_reach_serving_processes(self) -> None:
        result = render(self.values | {"INSTANCE_ID": "shared-identity-that-must-not-survive"})
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        for name in ("engine", "background"):
            for key in ("INSTANCE_ID", "PLATFORM_ADMIN_EMAIL", "PLATFORM_ADMIN_PASSWORD"):
                self.assertEqual(services[name]["environment"][key], "")
        self.assertEqual(services["bootstrap"]["environment"]["PLATFORM_ADMIN_EMAIL"],
                         self.values["PLATFORM_ADMIN_EMAIL"])

    def test_engine_and_ingress_route_only_readiness_eligible_backends(self) -> None:
        for name in ("engine", "background"):
            self.assertEqual(self.services[name]["healthcheck"]["test"],
                             ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/api/ready"])
        self.assertTrue(self.services["bootstrap"]["healthcheck"]["disable"])
        config = (ROOT / "deploy" / "haproxy.cfg").read_text()
        backend = config.split("\nbackend engine_pool\n", 1)[1].split("\nbackend ", 1)[0]
        self.assertIn("option httpchk GET /api/ready", backend)
        self.assertIn("http-check expect status 200", backend)
        self.assertIn("server-template engine", backend)
        self.assertNotIn("/api/health", backend)

    def test_external_urls_preserve_credentials_hosts_databases_and_tls_despite_shell_conflicts(self) -> None:
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://shell/wrong", "REDIS_URL": "redis://shell/3"}):
            result = render(self.values)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        for name in ("engine", "background", "bootstrap"):
            for key in ("DATABASE_URL", "REDIS_URL"):
                with self.subTest(service=name, key=key):
                    self.assertEqual(services[name]["environment"][key], self.values[key])

    def test_engine_security_overrides_and_host_guest_mounts(self) -> None:
        for name in ("engine", "background", "bootstrap"):
            with self.subTest(service=name):
                service = self.services[name]
                environment = service["environment"]
                for key in ("DEBUG", "ALLOW_MIGRATION_FAILURE", "SANDBOX_UNSAFE_FALLBACK",
                            "SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING"):
                    self.assertEqual(environment[key], "false")
                self.assertEqual(environment["AGENT_DATA_DIR"], "/srv/test-maraclaw/agents")
                self.assertEqual(environment["STORAGE_LOCAL_ROOT"], "/srv/test-maraclaw/agents")
                mounts = {mount["target"]: mount["source"] for mount in service["volumes"]}
                self.assertEqual(mounts["/data"], self.values["DATA_DIR"])
                self.assertEqual(mounts[self.values["DATA_DIR"]], self.values["DATA_DIR"])
                self.assertEqual(mounts["/var/run/docker.sock"], "/var/run/docker.sock")
                self.assertFalse(service.get("privileged", False))
                self.assertEqual(set(service["cap_add"]),
                                 {"SYS_ADMIN", "SETUID", "SETGID", "SYS_CHROOT", "SETPCAP", "NET_ADMIN", "SYS_PTRACE"})
                self.assertIn("seccomp=unconfined", service["security_opt"])
                self.assertFalse(any("no-new-privileges" in item for item in service["security_opt"]))

    def test_only_ingress_publishes_ports_and_owns_callback_alias(self) -> None:
        for name, service in self.services.items():
            with self.subTest(service=name):
                if name != "ingress":
                    self.assertFalse(service.get("ports"))
                for network in service["networks"].values():
                    if name != "ingress" and network:
                        self.assertNotIn("maraclaw-engine", network.get("aliases", []))
        ingress = self.services["ingress"]
        self.assertEqual({(port["published"], port["target"]) for port in ingress["ports"]},
                         {("80", 8080), ("443", 8443)})
        self.assertEqual(set(ingress["depends_on"]), {"engine"})
        self.assertEqual(ingress["depends_on"]["engine"]["condition"], "service_healthy")
        for network in ("app", "guest"):
            self.assertIn("maraclaw-engine", ingress["networks"][network]["aliases"])
        self.assertEqual(self.model["networks"]["guest"]["name"], "maraclaw_network")

    def test_frontend_builds_use_shared_default_and_remain_secret_free(self) -> None:
        for name in ("engine", "web-a", "web-e"):
            self.assertEqual(Path(self.services[name]["build"]["context"]), ROOT / name)
        for name in ("web-a", "web-e"):
            service = self.services[name]
            self.assertEqual(service["deploy"]["replicas"], 2)
            self.assertTrue(service["read_only"])
            self.assertFalse(service.get("env_file"))
            self.assertFalse(service.get("environment"))
            self.assertEqual(service["build"]["args"], {"NODE_VERSION": "26.11.1"})
        self.assertEqual(self.services["web-e"]["depends_on"]["ingress"]["condition"], "service_started")

    def test_frontend_builds_when_shared_node_version_is_overridden(self) -> None:
        version = "26.10.0"
        result = render(self.values | {"WEB_NODE_VERSION": version})
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        for name in ("web-a", "web-e"):
            self.assertEqual(services[name]["build"]["args"], {"NODE_VERSION": version})
            self.assertFalse(services[name].get("environment"))
        self.assertEqual(services["engine"]["build"], self.services["engine"]["build"])

    def test_tmpfs_options_remain_one_mount_when_compose_is_rendered(self) -> None:
        for name, size in (("web-a", "64m"), ("web-e", "64m")):
            with self.subTest(service=name):
                self.assertEqual(self.services[name]["tmpfs"], [f"/tmp:rw,noexec,nosuid,size={size}"])

    def test_external_stores_have_no_managed_services_networks_volumes_or_dependencies(self) -> None:
        self.assertEqual(set(self.services), {"engine", "background", "bootstrap", "web-a", "web-e", "ingress"})
        self.assertEqual(set(self.model["networks"]), {"app", "guest"})
        self.assertFalse(self.model.get("volumes"))
        for service in self.services.values():
            self.assertFalse({"postgres", "redis"} & service.get("depends_on", {}).keys())
            self.assertTrue(all(mount["type"] == "bind" for mount in service.get("volumes", [])))
        for name in ("engine", "background", "bootstrap"):
            self.assertEqual(set(self.services[name]["networks"]), {"app"})

    def test_example_image_tags_match_defaults_when_rendered(self) -> None:
        # Given shipped image settings; when rendered; then they match Compose defaults.
        tags = dict(line.split("=", 1) for line in (ROOT / "deploy" / ".env.example").read_text().splitlines()
                    if line.startswith(("HAPROXY_TAG=", "WEB_NODE_VERSION=")))
        result = render(self.values | tags)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        self.assertEqual(services["ingress"]["image"], self.services["ingress"]["image"])
        for name in ("web-a", "web-e"):
            self.assertEqual(services[name]["build"]["args"], self.services[name]["build"]["args"])

    def test_env_examples_require_canonical_urls_without_legacy_store_controls(self) -> None:
        for name in (".env.example", ".env.development.example"):
            values = dict(line.split("=", 1) for line in (ROOT / "deploy" / name).read_text().splitlines()
                          if line and not line.startswith("#"))
            self.assertEqual({key: values[key] for key in ("DATABASE_URL", "REDIS_URL")},
                             {"DATABASE_URL": "", "REDIS_URL": ""})
            self.assertFalse(any(key.startswith("POSTGRES_") or key.startswith("REDIS_") and key != "REDIS_URL"
                                 for key in values))

    def test_origins_and_callback_urls_are_derived_from_deployment(self) -> None:
        environment = self.services["engine"]["environment"]
        self.assertEqual(json.loads(environment["CORS_ORIGINS"]),
                         [f"https://{self.values[key]}" for key in ("API_DOMAIN", "ADMIN_DOMAIN", "MEMBER_DOMAIN")])
        self.assertEqual(environment["PUBLIC_BASE_URL"], "https://members.example.test")
        self.assertEqual(environment["OPENCLAW_ENGINE_BASE_URL"], "http://maraclaw-engine:8000")
        self.assertEqual(environment["LINKUP_PROXY_BASE_URL"], "http://maraclaw-engine:8000/api/linkup")

    def test_tls_bind_mount_cannot_silently_create_a_directory(self) -> None:
        tls = next(mount for mount in self.services["ingress"]["volumes"]
                   if mount["target"] == "/usr/local/etc/haproxy/tls.pem")
        self.assertEqual(tls["source"], self.values["TLS_PEM_PATH"])
        self.assertTrue(tls["read_only"])
        self.assertFalse(tls["bind"]["create_host_path"])


class BootstrapCleanup(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, startup_failure: bool) -> list[object]:
        events: list[object] = []

        class RuntimeProbe:
            async def start(self, roles: frozenset[str]) -> None:
                events.append(roles)
                if startup_failure:
                    raise RuntimeError("startup failure")

            async def stop(self) -> None:
                events.append("stopped")

        module = ModuleType("app.runtime.lifecycle")
        setattr(module, "Runtime", RuntimeProbe)
        with patch.dict("sys.modules", {"app.runtime.lifecycle": module}):
            script = runpy.run_path(str(ROOT / "deploy" / "bootstrap.py"))
            if startup_failure:
                with self.assertRaisesRegex(RuntimeError, "startup failure"):
                    await script["main"]()
            else:
                await script["main"]()
        return events

    async def test_cleanup_when_startup_succeeds(self) -> None:
        events = await self.exercise(False)
        self.assertEqual(events, [frozenset({"bootstrap"}), "stopped"])

    async def test_cleanup_when_startup_fails(self) -> None:
        events = await self.exercise(True)
        self.assertEqual(events, [frozenset({"bootstrap"}), "stopped"])


if __name__ == "__main__":
    unittest.main()

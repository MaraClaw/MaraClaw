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
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "SECRET_KEY", "JWT_SECRET_KEY", "POSTGRES_PASSWORD", "POSTGRES_PASSWORD_URLENCODED",
    "REDIS_PASSWORD", "REDIS_PASSWORD_URLENCODED", "PLATFORM_ADMIN_EMAIL",
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
            "POSTGRES_PASSWORD": "p@ ss:/#%$", "REDIS_PASSWORD": "r@ ss:/#%$",
            "PLATFORM_ADMIN_EMAIL": "admin@example.test", "PLATFORM_ADMIN_PASSWORD": "initial-password",
            "API_DOMAIN": "api.example.test", "ADMIN_DOMAIN": "admin.example.test",
            "MEMBER_DOMAIN": "members.example.test", "DATA_DIR": "/srv/test-maraclaw",
            "TLS_PEM_PATH": "/srv/test-tls.pem",
        }
        for prefix in ("POSTGRES", "REDIS"):
            cls.values[f"{prefix}_PASSWORD_URLENCODED"] = quote(cls.values[f"{prefix}_PASSWORD"], safe="")
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
                    result = render(values)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(key, result.stderr)

    def test_consumers_wait_for_completed_bootstrap_and_healthy_stores(self) -> None:
        for name in ("engine", "background"):
            with self.subTest(service=name):
                dependencies = self.services[name]["depends_on"]
                self.assertEqual(dependencies["bootstrap"]["condition"], "service_completed_successfully")
                for store in ("postgres", "redis"):
                    self.assertEqual(dependencies[store]["condition"], "service_healthy")
        self.assertNotIn("bootstrap", self.services["bootstrap"]["depends_on"])
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

    def test_runtime_password_urls_preserve_special_characters(self) -> None:
        environment = self.services["engine"]["environment"]
        self.assertEqual(environment["DATABASE_URL"],
                         f"postgresql://maraclaw:{quote(self.values['POSTGRES_PASSWORD'], safe='')}@postgres:5432/maraclaw")
        self.assertEqual(environment["REDIS_URL"],
                         f"redis://:{quote(self.values['REDIS_PASSWORD'], safe='')}@redis:6379/0")
        # Compose serializes literal dollars as $$ for safe config round-tripping.
        self.assertEqual(self.services["postgres"]["environment"]["POSTGRES_PASSWORD"],
                         self.values["POSTGRES_PASSWORD"].replace("$", "$$"))
        self.assertEqual(self.services["redis"]["environment"]["REDIS_PASSWORD"],
                         self.values["REDIS_PASSWORD"].replace("$", "$$"))

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
        self.assertTrue(self.model["networks"]["private"]["internal"])
        for store in ("postgres", "redis"):
            self.assertEqual(set(self.services[store]["networks"]), {"private"})

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
        for name, size in (("web-a", "64m"), ("web-e", "64m"), ("redis", "16m")):
            with self.subTest(service=name):
                self.assertEqual(self.services[name]["tmpfs"], [f"/tmp:rw,noexec,nosuid,size={size}"])

    def test_postgres_18_data_directory_is_inside_the_named_volume(self) -> None:
        # Given PostgreSQL 18's PGDATA; when Compose renders; then its parent is persisted.
        mounts = self.services["postgres"]["volumes"]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["type"], "volume")
        self.assertEqual(mounts[0]["source"], "postgres_data")
        self.assertEqual(mounts[0]["target"], "/var/lib/postgresql")
        self.assertTrue(Path("/var/lib/postgresql/18/docker").is_relative_to(mounts[0]["target"]))

    def test_example_image_tags_match_defaults_when_rendered(self) -> None:
        # Given shipped image settings; when rendered; then they match Compose defaults.
        tags = dict(line.split("=", 1) for line in (ROOT / "deploy" / ".env.example").read_text().splitlines()
                    if line.startswith(("POSTGRES_TAG=", "REDIS_TAG=", "HAPROXY_TAG=", "WEB_NODE_VERSION=")))
        result = render(self.values | tags)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        for name in ("postgres", "redis", "ingress"):
            with self.subTest(service=name):
                self.assertEqual(services[name]["image"], self.services[name]["image"])
        for name in ("web-a", "web-e"):
            self.assertEqual(services[name]["build"]["args"], self.services[name]["build"]["args"])

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

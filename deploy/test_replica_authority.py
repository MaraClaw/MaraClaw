from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from deploy import test_autoscale_docker as fixtures
from deploy.autoscale_docker import Docker
from deploy.autoscale_policy import GIB, Policy
from deploy.autoscale_state import Authority, Intent, Operation
from deploy.autoscale_lifecycle import Retirement
from deploy.test_autoscale_lifecycle import ProxyProbe


class AuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.BootstrapRerunTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.docker = self.fixture.docker
        self.state = self.docker.authority
        self.checkpoint = self.docker.state_dir / "startup.sha256"
        self.previous = self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns

    def fresh(self) -> Docker:
        with patch("deploy.autoscale_docker.run", return_value=fixtures.model()):
            return Docker(self.docker.root, (self.docker.env_file, *self.docker.files), self.docker.state_dir)

    def test_fresh_recorder_when_one_of_two_is_missing_cannot_adopt_observed_count(self) -> None:
        victim = self.fixture.members[0]
        members = tuple(item for item in self.fixture.members if item != victim)
        docker = self.fresh()
        with patch.object(docker, "containers", return_value=members), self.assertRaises(ValueError):
            docker.remember_startup()
        self.assertEqual((self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns), self.previous)

    def test_fresh_recorder_when_authorized_one_or_three_accepts_without_counting_defaults(self) -> None:
        original = self.fixture.members
        for count in (1, 3):
            with self.subTest(count=count):
                victim = next(item for item in original if item.service == "engine")
                members = tuple(item for item in original if item != victim) if count == 1 else (
                    *original, replace(victim, identity="f" * 64))
                self.state.save(Authority({"engine": count, "web-a": 2, "web-e": 2}))
                docker = self.fresh()
                with patch.object(docker, "containers", return_value=members):
                    docker.remember_startup()
                self.assertEqual(self.checkpoint.read_bytes(), self.previous[0])

    def test_record_when_authority_missing_malformed_foreign_or_unresolved_preserves_checkpoint(self) -> None:
        original = self.state.path.read_text()
        record = json.loads(original)
        for content in (None, "{", "[]", "null", json.dumps(record | {"version": True}),
                        json.dumps(record | {"counts": {"engine": 1}}),
                        json.dumps(record | {"counts": {"engine": True, "web-a": 2, "web-e": 2}})):
            with self.subTest(content=content):
                self.state.path.unlink(missing_ok=True)
                if content is not None:
                    self.state.path.write_text(content)
                with self.assertRaises((OSError, ValueError)):
                    self.docker.remember_startup()
                self.assertEqual((self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns), self.previous)
        for key, value in (("root", "/foreign"), ("project", "foreign"), ("state", "/foreign"),
                           ("endpoint", "unix:///foreign.sock"), ("env", "/foreign.env"),
                           ("files", ["/foreign.yaml"])):
            with self.subTest(binding=key):
                changed = json.loads(original)
                changed["binding"][key] = value
                self.state.path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    self.docker.remember_startup()
                self.assertEqual((self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns), self.previous)
        self.state.path.write_text(original)
        victim = Intent("a" * 64, "10.0.0.2", "engine", "engine_pool", "engine1", "generation")
        for phase in ("draining", "removing", "releasing"):
            changed = json.loads(original)
            changed["operation"] = {"phase": phase, "victim": {
                "identity": victim.identity, "ip": victim.ip, "service": victim.service,
                "pool": victim.pool, "slot": victim.slot, "generation": victim.generation}}
            self.state.path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                self.docker.remember_startup()
            self.assertEqual((self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns), self.previous)

    def test_startup_when_known_targets_have_shortage_restores_target_not_defaults_or_observation(self) -> None:
        self.state.save(Authority({"engine": 3, "web-a": 1, "web-e": 2}))
        self.fixture.members = tuple(item for item in self.fixture.members if item.service not in {"engine", "web-a"})
        with patch.object(self.docker, "memory", return_value=(32 * GIB, GIB, 0)):
            self.assertEqual(self.docker.startup(Policy()), {"engine": 3, "web-a": 1, "web-e": 2})
        self.assertFalse(self.docker.startup_reusable)

    def test_startup_when_existing_count_exceeds_authority_refuses_unsafe_scale_down(self) -> None:
        self.state.save(Authority({"engine": 1, "web-a": 2, "web-e": 2}))
        with self.assertRaisesRegex(ValueError, "unsafe Compose scale-down"):
            self.docker.startup(Policy())

    def test_startup_when_initial_memory_refused_does_not_publish_intent(self) -> None:
        self.state.path.unlink()
        with patch.object(self.docker, "memory", return_value=(8 * GIB, GIB, 0)), self.assertRaises(ValueError):
            self.docker.startup(Policy())
        self.assertFalse(self.state.path.exists())

    def test_startup_when_compose_fails_retains_admitted_target_for_next_instance(self) -> None:
        self.state.path.unlink()
        with patch.object(self.docker, "memory", return_value=(32 * GIB, GIB, 0)):
            counts = self.docker.startup(Policy())
        with patch("deploy.autoscale_docker.run", side_effect=RuntimeError("compose failed")):
            with self.assertRaises(RuntimeError):
                self.docker.grow("engine", counts["engine"])
        self.fixture.members = ()
        with patch.object(self.docker, "memory", return_value=(32 * GIB, 0, 0)):
            self.assertEqual(self.docker.startup(Policy()), counts)

    def test_legacy_pending_when_no_authority_never_authorizes_direct_record(self) -> None:
        self.state.path.unlink()
        (self.state.path.parent / "pending.json").write_text("{}")
        with self.assertRaises(OSError):
            self.docker.remember_startup()
        self.assertEqual((self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns), self.previous)

    def test_startup_when_removed_victim_is_absent_reconciles_without_proxy_then_requires_cleanup_before_record(self) -> None:
        victim = self.fixture.members[0]
        intent = Intent(victim.identity, victim.ip, victim.service, "engine_pool", "engine1", "generation")
        self.state.save(Authority(self.state.load().counts, Operation("removing", intent)))
        self.fixture.members = tuple(item for item in self.fixture.members if item != victim)
        self.assertEqual(self.docker.startup(Policy())["engine"], 1)
        with self.assertRaises(ValueError):
            self.docker.remember_startup()
        proxy = ProxyProbe()
        recovery = Retirement(self.docker.state_dir, proxy, self.docker)
        with patch.object(proxy, "release", side_effect=OSError("offline")), self.assertRaises(OSError):
            recovery.recover()
        self.assertEqual((self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns), self.previous)
        recovery.recover()
        self.docker.remember_startup()
        self.assertEqual(self.state.settled().counts["engine"], 1)

    def test_startup_when_removing_victim_is_stopped_restores_original_count(self) -> None:
        victim = self.fixture.members[0]
        intent = Intent(victim.identity, victim.ip, victim.service, "engine_pool", "engine1", "generation")
        self.state.save(Authority(self.state.load().counts, Operation("removing", intent)))
        self.fixture.members = tuple(replace(item, running=False) if item == victim else item for item in self.fixture.members)
        self.assertEqual(self.docker.startup(Policy())["engine"], 2)
        self.assertFalse(self.docker.startup_reusable)

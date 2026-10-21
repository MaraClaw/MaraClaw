from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from deploy.autoscale_state import Authority, Binding, ReplicaState
from deploy import test_development as development


class ReplicaStateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.binding = Binding("/repo", "test", str(self.directory), "unix:///fixture/docker.sock",
                               "/input.env", ("/base.yaml", "/overlay.yaml"))
        self.state = ReplicaState(self.binding)
        self.state.save(Authority({"engine": 2, "web-a": 2, "web-e": 2}))

    def test_load_when_ordered_compose_files_are_reversed_refuses_foreign_authority(self) -> None:
        foreign = ReplicaState(replace(self.binding, files=tuple(reversed(self.binding.files))))
        with self.assertRaises(ValueError):
            foreign.load()

    def test_load_when_unknown_fields_or_duplicate_keys_appear_refuses_without_modifying_evidence(self) -> None:
        original = self.state.path.read_text()
        for content in (original.replace('"version": 1', '"version": 1, "version": 1'),
                        json.dumps(json.loads(original) | {"version": 2}),
                        json.dumps(json.loads(original) | {"unknown": True})):
            with self.subTest(content=content):
                self.state.path.write_text(content)
                previous = self.state.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.state.load()
                self.assertEqual(self.state.path.read_bytes(), previous)

    def test_state_when_endpoint_is_not_local_unix_refuses_authority(self) -> None:
        for endpoint in ("tcp://localhost:2375", "ssh://host", "unix://host/var/run/docker.sock",
                         "unix:relative.sock", "unix:///fixture/docker.sock?alternate=1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                ReplicaState(replace(self.binding, endpoint=endpoint))

    def test_load_when_unix_endpoint_has_canonical_alias_uses_same_authority(self) -> None:
        alias = ReplicaState(replace(self.binding, endpoint="unix:///fixture/sub/../docker.sock"))
        self.assertEqual(alias.load().counts, self.state.load().counts)

    def test_startup_when_authority_malformed_cannot_readopt_observed_counts(self) -> None:
        from deploy import test_autoscale_docker as fixtures
        fixture = fixtures.BootstrapRerunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state = fixture.docker.authority
        state.path.write_text("{malformed")
        previous = state.path.read_bytes()
        with self.assertRaises(ValueError):
            fixture.docker.startup(development.Policy())
        self.assertEqual(state.path.read_bytes(), previous)

    def test_development_when_intent_exists_without_checkpoint_cannot_recreate_missing_effective_state(self) -> None:
        fixture = development.DevelopmentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.docker()
        (fixture.state / "replicas.json").write_text("existing authority")
        (fixture.state / "development.json").unlink()
        with self.assertRaisesRegex(ValueError, "override missing"):
            fixture.docker()

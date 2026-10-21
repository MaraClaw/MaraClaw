from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from deploy.autoscale_lifecycle import Retirement
from deploy.autoscale_policy import POOLS
from deploy.autoscale_state import Authority, Intent, Operation, atomic_write
from deploy import test_autoscale_lifecycle as lifecycle
from deploy import test_autoscale_controller as controller


class RetirementCrashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = lifecycle.RetirementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.retirement = self.fixture.retirement
        self.docker = self.fixture.docker
        self.proxy = self.fixture.proxy
        self.state = self.docker.authority
        self.fixture.begin()
        self.retirement.advance(self.fixture.maintaining(), 1, self.fixture.maintaining)

    def recover(self) -> None:
        Retirement(self.state.path.parent, self.proxy, self.docker).recover()

    def advance(self) -> None:
        self.retirement.advance(self.fixture.maintaining(), 2, self.fixture.maintaining)

    def test_crash_when_removal_authorization_not_published_never_stops_or_decrements(self) -> None:
        with patch.object(self.state.__class__, "save", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.advance()
        self.assertEqual(self.docker.removed, [])
        self.recover()
        self.assertEqual(self.state.settled().counts["engine"], 2)

    def test_crash_when_removing_published_before_stop_preserves_target_without_resuming_delete(self) -> None:
        with patch.object(self.docker, "remove", side_effect=OSError("before stop")):
            with self.assertRaises(OSError):
                self.advance()
        self.retirement.cancel()
        operation = self.state.load().operation
        self.assertIsNotNone(operation)
        assert operation is not None
        self.assertEqual(operation.phase, "removing")
        self.recover()
        self.assertEqual(self.state.settled().counts["engine"], 2)
        self.assertEqual(self.docker.removed, [])

    def test_crash_when_stop_completed_but_rm_failed_counts_stopped_victim_and_restores_slot(self) -> None:
        def stop_only(identity: str) -> None:
            self.docker.current = tuple(replace(item, running=False) if item.identity == identity else item
                                        for item in self.docker.current)
            raise OSError("after stop")
        with patch.object(self.docker, "remove", side_effect=stop_only), self.assertRaises(OSError):
            self.advance()
        self.recover()
        self.assertEqual(self.state.settled().counts["engine"], 2)
        self.assertEqual(self.docker.removed, [])
        self.assertEqual(self.proxy.states[-1][-1], "ready")

    def test_crash_when_rm_completed_before_accounting_decrements_once_and_never_resumes_delete(self) -> None:
        def remove_then_crash(identity: str) -> None:
            self.docker.current = tuple(item for item in self.docker.current if item.identity != identity)
            raise OSError("after rm")
        with patch.object(self.docker, "remove", side_effect=remove_then_crash), self.assertRaises(OSError):
            self.advance()
        self.retirement.cancel()
        self.recover()
        self.recover()
        self.assertEqual(self.state.settled().counts["engine"], 1)
        self.assertEqual(self.docker.removed, [])

    def test_crash_when_release_fails_keeps_atomic_decrement_and_retries_only_cleanup(self) -> None:
        with patch.object(self.proxy, "release", side_effect=OSError("proxy unavailable")), self.assertRaises(OSError):
            self.advance()
        authority = self.state.load()
        self.assertEqual(authority.counts["engine"], 1)
        assert authority.operation is not None
        self.assertEqual(authority.operation.phase, "releasing")
        self.retirement.cancel()
        self.recover()
        self.recover()
        self.assertEqual(self.state.settled().counts["engine"], 1)
        self.assertEqual(self.docker.removed, ["b" * 64])

    def test_crash_when_decrement_replace_is_interrupted_before_or_after_commit_never_decrements_twice(self) -> None:
        for committed in (False, True):
            with self.subTest(committed=committed):
                fixture = lifecycle.RetirementTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                state = fixture.docker.authority
                fixture.begin()
                fixture.retirement.advance(fixture.maintaining(), 1, fixture.maintaining)
                save = state.save
                def interrupt(authority: Authority) -> None:
                    assert authority.operation is not None
                    if authority.operation.phase == "releasing":
                        if committed:
                            save(authority)
                        raise OSError("accounting crash")
                    save(authority)
                with patch.object(state, "save", side_effect=interrupt), self.assertRaises(OSError):
                    fixture.retirement.advance(fixture.maintaining(), 2, fixture.maintaining)
                recovered = Retirement(state.path.parent, fixture.proxy, fixture.docker)
                recovered.recover()
                recovered.recover()
                self.assertEqual(state.settled().counts["engine"], 1)
                self.assertEqual(fixture.docker.removed, ["b" * 64])

    def test_reconciliation_when_proxy_unavailable_finishes_accounting_without_contacting_proxy(self) -> None:
        authority = self.state.load()
        assert authority.operation is not None
        self.state.save(replace(authority, operation=replace(authority.operation, phase="removing")))
        victim = authority.operation.victim.identity
        self.docker.current = tuple(item for item in self.docker.current if item.identity != victim)
        with patch.object(self.proxy, "release", side_effect=OSError("offline")) as release:
            observed = {name: len(self.docker.owned(self.docker.current, name)) for name in POOLS}
            reconciled = self.state.reconcile(observed, {item.identity for item in self.docker.current})
        release.assert_not_called()
        self.assertEqual(reconciled.counts["engine"], 1)
        assert reconciled.operation is not None
        self.assertEqual(reconciled.operation.phase, "releasing")

    def test_recovery_when_inventory_fails_preserves_removing_evidence(self) -> None:
        authority = self.state.load()
        assert authority.operation is not None
        self.state.save(replace(authority, operation=replace(authority.operation, phase="removing")))
        previous = self.state.path.read_bytes()
        with patch.object(self.docker, "containers", side_effect=OSError("inspect failed")), self.assertRaises(OSError):
            self.recover()
        self.assertEqual(self.state.path.read_bytes(), previous)
        self.assertEqual(self.docker.removed, [])

    def test_recovery_when_unknown_phase_refuses_without_erasing_evidence(self) -> None:
        record = json.loads(self.state.path.read_text())
        record["operation"]["phase"] = "unknown"
        self.state.path.write_text(json.dumps(record))
        previous = self.state.path.read_bytes()
        with self.assertRaises(ValueError):
            self.recover()
        self.assertEqual(self.state.path.read_bytes(), previous)
        self.assertEqual(self.docker.removed, [])

    def test_legacy_pending_when_recovered_is_release_only_without_count_change(self) -> None:
        authority = self.state.load()
        assert authority.operation is not None
        self.state.save(Authority(authority.counts))
        pending = self.state.path.parent / "pending.json"
        pending.write_text(json.dumps(asdict(authority.operation.victim)))
        self.recover()
        self.assertEqual(self.state.settled().counts["engine"], 2)
        self.assertEqual(self.docker.removed, [])
        self.assertFalse(pending.exists())


class GrowthCrashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = controller.ControllerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.runtime = self.fixture.runtime
        self.ingress = self.fixture.ingress
        self.ingress.requests = 1000
        self.controller.tick(1)
        self.ingress.requests = 2000

    def test_growth_when_authority_write_fails_never_runs_compose(self) -> None:
        with patch.object(self.runtime.authority, "save", side_effect=OSError("authorization")):
            with self.assertRaises(OSError):
                self.controller.tick(2)
        self.assertEqual(self.runtime.grown, [])
        self.assertEqual(self.runtime.authority.settled().counts["engine"], 2)

    def test_growth_when_compose_fails_retains_higher_target_and_suspends_all_scaling(self) -> None:
        with patch.object(self.runtime, "grow", side_effect=OSError("compose failed")), self.assertRaises(OSError):
            self.controller.tick(2)
        self.assertEqual(self.runtime.authority.settled().counts["engine"], 3)
        self.controller.invalidate()
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.controller.tick(3)
        self.assertEqual(self.runtime.grown, [])
        self.assertEqual(self.runtime.removed, [])

    def test_tick_when_observed_excess_exists_suspends_all_scaling(self) -> None:
        self.runtime.authority.save(Authority({"engine": 1, "web-a": 2, "web-e": 2}))
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.controller.tick(2)
        self.assertEqual(self.runtime.grown, [])

    def test_releasing_when_proxy_recovers_retries_cleanup_without_scaling_or_decrement(self) -> None:
        victim = self.runtime.owned(self.runtime.current, "engine")[0]
        intent = Intent(victim.identity, victim.ip, "engine", "engine_pool", "engine1", "generation")
        counts = self.runtime.authority.load().counts
        self.runtime.authority.save(Authority(counts, Operation("releasing", intent)))
        with patch.object(self.ingress, "release", side_effect=OSError("proxy offline")), self.assertRaises(OSError):
            self.controller.tick(2)
        self.controller.invalidate()
        self.assertEqual(self.controller.tick(3), "recovered")
        self.assertEqual(self.runtime.authority.settled().counts, counts)
        self.assertEqual(self.runtime.grown, [])
        self.assertEqual(self.runtime.removed, [])


class AtomicStateTests(unittest.TestCase):
    def test_atomic_replace_when_interrupted_preserves_previous_file_and_cleans_private_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "replicas.json"
            path.write_text("previous")
            with patch("deploy.autoscale_state.os.replace", side_effect=OSError("crash")), self.assertRaises(OSError):
                atomic_write(path, "next")
            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_atomic_replace_when_successful_syncs_file_then_directory_and_keeps_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "replicas.json"
            events: list[str] = []
            sync, move = os.fsync, os.replace
            def sync_descriptor(descriptor: int) -> None:
                events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
                sync(descriptor)
            def replace_file(source: str, target: Path) -> None:
                events.append("replace")
                move(source, target)
            with patch("deploy.autoscale_state.os.fsync", side_effect=sync_descriptor), \
                    patch("deploy.autoscale_state.os.replace", side_effect=replace_file):
                atomic_write(path, "next")
            self.assertEqual(events, ["file", "replace", "directory"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

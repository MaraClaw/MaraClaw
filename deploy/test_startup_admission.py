from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy import autoscale, starter, starter_service
from deploy.autoscale_docker import Docker
from deploy.autoscale_policy import GIB
from deploy.test_autoscale_docker import container, model


class AdmissionEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = self.root / "policy.toml"
        self.policy.write_text("")
        self.state = self.root / "state"
        manager = patch("deploy.starter_handoff.manager_directory", return_value=self.root / "supervisor")
        manager.start()
        self.addCleanup(manager.stop)

    def test_starter_when_admission_fails_reports_reason_without_start_record_or_install(self) -> None:
        args = ["starter", "--root", str(self.root), "--policy", str(self.policy),
                "--state-dir", str(self.state), "--no-build"]
        guest = replace(container(), project="guest", memory=0)
        for members, total, detail in (((), 8 * GIB, "shortfall=2.750 GiB"),
                                       ((guest,), 32 * GIB, "external_reserve=unknown")):
            with self.subTest(detail=detail):
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(sys, "argv", args), redirect_stdout(output), redirect_stderr(errors), \
                        patch.dict(os.environ, {}, clear=False), \
                        patch.object(starter, "prerequisites"), \
                        patch.object(starter.os, "uname", return_value=Mock(sysname="Darwin")), \
                        patch("deploy.autoscale_docker.run", side_effect=[model(), model(), str(total)]), \
                        patch.object(Docker, "containers", return_value=members), \
                        patch.object(starter, "run"), patch.object(starter, "prepare") as development, \
                        patch.object(starter, "execute") as execute, \
                        patch.object(Docker, "remember_startup") as record, \
                        patch.object(starter_service, "stop"), patch.object(starter_service, "install") as install:
                    status = starter.main()
                self.assertEqual(status, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("required_stack=6.750 GiB", errors.getvalue())
                self.assertIn(detail, errors.getvalue())
                execute.assert_not_called()
                record.assert_not_called()
                install.assert_not_called()
                development.assert_not_called()

    def test_starter_when_unrelated_error_contains_sensitive_details_sanitizes_output(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["starter"]), redirect_stdout(output), redirect_stderr(errors), \
                patch.object(starter, "prerequisites", side_effect=ValueError("secret-env-value")):
            status = starter.main()
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("startup refused", errors.getvalue())
        self.assertNotIn("secret-env-value", errors.getvalue())

    def test_check_startup_when_refused_reports_safe_details_without_stdout_or_record(self) -> None:
        guest = replace(container(), project="guest", memory=0)
        for members, total, detail in (((), 8 * GIB, "shortfall=2.750 GiB"),
                                       ((guest,), 32 * GIB, "external_reserve=unknown")):
            with self.subTest(detail=detail):
                with ExitStack() as stack:
                    output = stack.enter_context(redirect_stdout(io.StringIO()))
                    stack.enter_context(patch.dict(os.environ, {}, clear=False))
                    stack.enter_context(patch.object(sys, "argv", ["autoscale", "--check-startup", "--root",
                        str(self.root), "--policy", str(self.policy), "--state-dir", str(self.state)]))
                    stack.enter_context(patch("deploy.autoscale_docker.run", side_effect=[model(), str(total)]))
                    stack.enter_context(patch.object(Docker, "containers", return_value=members))
                    record = stack.enter_context(patch.object(Docker, "remember_startup"))
                    logs = stack.enter_context(self.assertLogs("maraclaw-autoscaler", level="ERROR"))
                    status = autoscale.main()
                self.assertEqual(status, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn(detail, "\n".join(logs.output))
                record.assert_not_called()

    def test_check_startup_when_unrelated_error_contains_sensitive_details_keeps_it_sanitized(self) -> None:
        with patch.object(sys, "argv", ["autoscale", "--check-startup"]), \
                patch.dict(os.environ, {}, clear=False), \
                patch.object(autoscale.Policy, "load", side_effect=ValueError("secret-env-value")), \
                self.assertLogs("maraclaw-autoscaler", level="ERROR") as logs:
            status = autoscale.main()
        self.assertEqual(status, 1)
        self.assertIn("controller_refused", "\n".join(logs.output))
        self.assertNotIn("secret-env-value", "\n".join(logs.output))

    def test_check_startup_when_admitted_prints_only_preserved_scale_arguments(self) -> None:
        members = tuple(replace(container(), identity=f"{index:064x}", running=False) for index in (1, 2, 3))
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["autoscale", "--check-startup", "--root", str(self.root),
                "--policy", str(self.policy), "--state-dir", str(self.state)]), \
                redirect_stdout(output), redirect_stderr(errors), patch.dict(os.environ, {}, clear=False), \
                patch("deploy.autoscale_docker.run", side_effect=[model(), str(24 * GIB)]), \
                patch.object(Docker, "containers", return_value=members):
            status = autoscale.main()
        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), "--scale engine=3 --scale web-a=2 --scale web-e=2\n")
        self.assertEqual(errors.getvalue(), "")

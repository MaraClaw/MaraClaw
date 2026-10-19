from dataclasses import replace
import subprocess
import unittest
from unittest.mock import Mock, patch

from deploy.autoscale_docker import Container
from deploy.autoscale_transport import ExecProxy, RELAY


class ExecTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.members = tuple(Container(letter * 64, "owned", name, True, True, "10.0.0.1", 128, "start")
                             for letter, name in (("a", "background"), ("b", "ingress")))
        self.docker = Mock()
        self.docker.containers.side_effect = lambda: self.members
        self.docker.owned.side_effect = lambda members, service: tuple(item for item in members
                                                                 if item.service == service and item.project == "owned")
        self.proxy = ExecProxy(self.docker)

    def test_command_uses_fresh_full_identity_stdin_argv_and_deadline(self) -> None:
        with patch("deploy.autoscale_transport.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"stats", b"")) as run:
            self.assertEqual(self.proxy.command("show stat"), "stats")
        command = run.call_args.args[0]
        self.assertEqual(command[1:], ["exec", "-i", "a" * 64, "python", "-c", RELAY])
        self.assertEqual(run.call_args.kwargs["input"], b"show stat\n")
        self.assertEqual(run.call_args.kwargs["timeout"], 8)

    def test_unhealthy_duplicate_or_unowned_background_never_executes(self) -> None:
        original = self.members
        for members in ((replace(original[0], healthy=False), original[1]),
                        (replace(original[0], project="other"), original[1]), original + (original[0],)):
            self.members = members
            with patch("deploy.autoscale_transport.subprocess.run") as run, self.assertRaises(ValueError):
                self.proxy.command("show info")
            run.assert_not_called()

    def test_ingress_change_during_sample_refuses_mutation(self) -> None:
        self.proxy.ingress = "b" * 64 + ":old-start"
        with patch("deploy.autoscale_transport.subprocess.run") as run, self.assertRaises(ValueError):
            self.proxy.state("engine_pool", "engine1", "maint")
        run.assert_not_called()

    def test_nonzero_or_oversize_transport_cannot_be_parsed_as_valid_sample(self) -> None:
        for status, response in ((1, b""), (0, b"x" * (8 * 1024**2 + 1))):
            with patch("deploy.autoscale_transport.subprocess.run", return_value=subprocess.CompletedProcess([], status, response, b"")), \
                    self.assertRaises(RuntimeError):
                self.proxy.command("show stat")

    def test_multiline_command_is_refused_before_exec(self) -> None:
        with patch("deploy.autoscale_transport.subprocess.run") as run, self.assertRaises(ValueError):
            self.proxy.command("show stat\nshutdown sessions server engine_pool/engine1")
        run.assert_not_called()

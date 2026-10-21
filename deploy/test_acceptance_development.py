import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from deploy import acceptance_development as acceptance


class AcceptanceDevelopmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="acceptance-boundary-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.external = self.directory / "external.env"
        self.external.write_text("DATABASE_URL=postgresql://qa:db-secret@db/qa\nREDIS_URL=redis://:cache-secret@cache/0\n")
        self.external.chmod(0o600)
        self.receipt = self.directory / "receipt.json"
        self.options = ["--run", "--confirm-disposable-stores", "--external-env-file", str(self.external),
                        "--receipt", str(self.receipt)]
        self.addCleanup(os.umask, os.umask(0o077))
        environment = patch.dict(os.environ, {"DATABASE_URL": "postgresql://operator/production",
            "REDIS_URL": "redis://operator/0", "MARACLAW_ENV_FILE": str(self.directory / "operator.env"),
            "COMPOSE_PROJECT_NAME": "operator", "PLATFORM_ADMIN_PASSWORD": "operator-password"})
        environment.start()
        self.addCleanup(environment.stop)

    def reject(self, options: list[str]) -> None:
        with patch("sys.argv", ["acceptance", *options]), contextlib.redirect_stderr(io.StringIO()) as errors, \
                patch("deploy.starter_host.prerequisites") as prerequisites, \
                patch.object(acceptance, "exercise") as exercise, \
                patch.object(acceptance.subprocess, "run") as command, patch.object(acceptance.subprocess, "Popen") as process, \
                self.assertRaises(SystemExit) as failure:
            acceptance.main()
        self.assertEqual(failure.exception.code, 2)
        for boundary in (prerequisites, exercise, command, process):
            boundary.assert_not_called()
        self.assertFalse(self.receipt.exists())
        self.assertNotIn("db-secret", errors.getvalue())
        self.assertNotIn("cache-secret", errors.getvalue())

    def test_missing_required_flags_fail_before_prerequisites(self) -> None:
        for missing in ("--run", "--confirm-disposable-stores", "--external-env-file"):
            options = self.options.copy()
            index = options.index(missing)
            del options[index:index + (2 if missing == "--external-env-file" else 1)]
            with self.subTest(missing=missing):
                self.reject(options)

    def test_missing_file_fails_before_prerequisites(self) -> None:
        self.external.unlink()
        self.reject(self.options)

    def test_absent_or_blank_urls_never_fall_back_to_ambient(self) -> None:
        for content in ("", "DATABASE_URL=postgresql://qa/db\n", "REDIS_URL=redis://cache/0\n",
                        "DATABASE_URL= # empty\nREDIS_URL=redis://cache/0\n",
                        "DATABASE_URL=postgresql://qa/db\nREDIS_URL='   '\n"):
            self.external.write_text(content)
            with self.subTest(content=content):
                self.reject(self.options)

    def test_unprotected_or_nonregular_files_fail_before_prerequisites(self) -> None:
        self.external.chmod(0o644)
        self.reject(self.options)
        self.external.unlink()
        self.external.symlink_to(self.directory / "missing.env")
        self.reject(self.options)
        self.external.unlink()
        self.external.mkdir()
        self.reject(self.options)

    def test_ambiguous_or_malformed_urls_fail_before_prerequisites(self) -> None:
        for value in ("'unterminated", '"unterminated', "'url' trailing", '"url\\nvalue"', "'url\x00value'",
                      "url\nDATABASE_URL=duplicate"):
            self.external.write_text(f"DATABASE_URL={value}\nREDIS_URL=redis://cache/0\n")
            with self.subTest(value=value):
                self.reject(self.options)

    def test_dotenv_quoting_preserves_literal_urls(self) -> None:
        self.external.write_text(r'''# Do not import unrelated operator settings.
export DATABASE_URL = 'postgresql://qa:p%25$TOKEN#it\'s"@db/qa?x=${AMBIENT}' # comment
REDIS_URL="redis://:p%25\$TOKEN#\"quote\"\\path@cache/0" # comment
PLATFORM_ADMIN_PASSWORD=operator-password
''')
        result = acceptance.external_stores(self.external)
        self.assertEqual(result, {"DATABASE_URL": 'postgresql://qa:p%25$TOKEN#it\'s"@db/qa?x=${AMBIENT}',
            "REDIS_URL": 'redis://:p%25$TOKEN#"quote"\\path@cache/0'})

    def test_unquoted_dollars_and_hashes_are_literal(self) -> None:
        self.external.write_text("DATABASE_URL=postgresql://qa:$PASSWORD%25#fragment@db/qa # comment\n"
                                 "REDIS_URL=redis://:$PASSWORD%25#fragment@cache/0\n")
        self.assertEqual(acceptance.external_stores(self.external), {
            "DATABASE_URL": "postgresql://qa:$PASSWORD%25#fragment@db/qa",
            "REDIS_URL": "redis://:$PASSWORD%25#fragment@cache/0"})

    def test_startup_failure_keeps_urls_private_and_cleans_only_owned_application(self) -> None:
        stores = acceptance.StoreURLs(DATABASE_URL='postgresql://qa:p%25$TOKEN#\'"\\@db/qa',
                                     REDIS_URL='redis://:p%25${TOKEN}#\'"\\@cache/0')
        for name in ("key.pem", "cert.pem"):
            (self.directory / name).write_text("synthetic TLS fixture")
        original = self.external.read_bytes()
        process = Mock()
        process.poll.return_value = 1
        project_uuid = uuid.UUID("12345678123456781234567812345678")
        with patch.object(acceptance.uuid, "uuid4", return_value=project_uuid), \
                patch.object(acceptance.subprocess, "run"), \
                patch.object(acceptance.subprocess, "Popen", return_value=process) as launch, \
                patch.object(acceptance, "run", side_effect=[json.dumps({"services": {name: {} for name in acceptance.APPLICATION_SERVICES}}), ""]) as docker, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                self.assertRaisesRegex(RuntimeError, "foreground startup failed"):
            acceptance.exercise(self.directory, stores)
        synthetic = self.directory / "synthetic.env"
        encoded = dict(line.split("=", 1) for line in synthetic.read_text().splitlines())
        self.assertEqual(encoded["DATABASE_URL"], r'''"postgresql://qa:p%25$$TOKEN#'\"\\@db/qa"''')
        self.assertEqual(encoded["REDIS_URL"], r'''"redis://:p%25$${TOKEN}#'\"\\@cache/0"''')
        self.assertEqual(synthetic.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("POSTGRES_PASSWORD", encoded)
        self.assertNotEqual(json.loads(encoded["PLATFORM_ADMIN_PASSWORD"]), "operator-password")
        for key in ("SECRET_KEY", "JWT_SECRET_KEY", "PLATFORM_ADMIN_PASSWORD"):
            self.assertGreaterEqual(len(json.loads(encoded[key])), 40)
            self.assertNotIn(json.loads(encoded[key]), output.getvalue())
        inherited = launch.call_args.kwargs["env"]
        self.assertEqual(inherited["MARACLAW_ENV_FILE"], str(synthetic))
        self.assertTrue({"DATABASE_URL", "REDIS_URL", "COMPOSE_PROJECT_NAME", "PLATFORM_ADMIN_PASSWORD"}.isdisjoint(inherited))
        commands = [operation.args[0] for operation in docker.call_args_list]
        cleanup = commands[-1]
        self.assertEqual(cleanup[cleanup.index("--project-name") + 1], "maraclaw-dev-qa-1234567812")
        self.assertEqual(cleanup[-4:], ["down", "--volumes", "--timeout", "60"])
        self.assertNotIn("--remove-orphans", cleanup)
        self.assertEqual(len(commands), 2)
        self.assertIn("config", commands[0])
        self.assertEqual(self.external.read_bytes(), original)
        self.assertNotIn(stores["DATABASE_URL"], output.getvalue())
        self.assertNotIn(stores["REDIS_URL"], output.getvalue())

    def test_bundled_store_model_is_rejected_without_launch_or_cleanup(self) -> None:
        for name in ("key.pem", "cert.pem"):
            (self.directory / name).write_text("synthetic TLS fixture")
        services = {name: {} for name in acceptance.APPLICATION_SERVICES | {"postgres", "redis"}}
        with patch.object(acceptance.subprocess, "run"), patch.object(acceptance.subprocess, "Popen") as launch, \
                patch.object(acceptance, "run", return_value=json.dumps({"services": services})) as docker, \
                self.assertRaisesRegex(ValueError, "application-only"):
            acceptance.exercise(self.directory, acceptance.external_stores(self.external))
        launch.assert_not_called()
        self.assertEqual(docker.call_count, 1)
        self.assertIn("config", docker.call_args.args[0])

    def test_qualification_preserves_readiness_auth_rerun_and_growth(self) -> None:
        project = "maraclaw-dev-qa-1234567812"
        for name in ("key.pem", "cert.pem"):
            (self.directory / name).write_text("synthetic TLS fixture")
        state = self.directory / "state"
        state.mkdir()
        for name in ("startup.sha256", "development.json"):
            (state / name).write_text("unchanged checkpoint fixture")
        container = SimpleNamespace(identity="owned-container", project=project, started="start-generation")
        docker = Mock(project=project, state_dir=state, compose=["docker", "compose"])
        docker.containers.return_value = [container]
        docker.owned.return_value = [container]
        docker.memory.return_value = (16 * acceptance.GIB, 3 * acceptance.GIB, 0)
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        proxy = Mock()
        proxy.snapshot.return_value.rows = {(pool, "BACKEND"): {} for pool, _ in acceptance.POOLS.values()}
        services = {name: {} for name in acceptance.APPLICATION_SERVICES}
        with patch.object(acceptance.uuid, "uuid4", return_value=uuid.UUID("12345678123456781234567812345678")), \
                patch.object(acceptance.subprocess, "run") as command, \
                patch.object(acceptance.subprocess, "Popen", return_value=process) as launch, \
                patch.object(acceptance, "run", side_effect=[json.dumps({"services": services}), json.dumps({"services": services}), "127.0.0.1:12345", ""]) as operations, \
                patch.object(acceptance, "Docker", return_value=docker), \
                patch.object(acceptance, "ExecProxy", return_value=proxy), \
                patch.object(acceptance, "request", return_value=(200, b"ready")) as readiness, \
                patch.object(acceptance, "authenticate") as authenticate, \
                patch.object(acceptance, "inspect_health", return_value=[]) as health, \
                patch.object(acceptance, "independent_growth", return_value=["growth"]) as growth, \
                contextlib.redirect_stdout(io.StringIO()):
            receipt = acceptance.exercise(self.directory, acceptance.external_stores(self.external))
        self.assertEqual(readiness.call_args_list, [call(12345, "engine.local", "/api/ready"),
            call(12345, "admin.local", "/"), call(12345, "member.local", "/")])
        authenticate.assert_called_once()
        growth.assert_called_once_with(docker, proxy, 12345)
        self.assertEqual(health.call_count, 2)
        self.assertTrue(receipt["checkpoint_reused"])
        self.assertTrue(receipt["https_and_auth"])
        self.assertEqual(receipt["growth"], ["growth"])
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args.args[0], launch.call_args.args[0][:-1])
        self.assertEqual(command.call_args.kwargs["env"], launch.call_args.kwargs["env"])
        self.assertIs(command.call_args.kwargs["stdout"], command.call_args.kwargs["stderr"])
        self.assertEqual(operations.call_args.args[0][-4:], ["down", "--volumes", "--timeout", "60"])

    def test_receipt_reports_caller_owned_preserved_stores_without_secrets(self) -> None:
        stores = acceptance.external_stores(self.external)
        with patch("sys.argv", ["acceptance", *self.options]), \
                patch.object(acceptance.os, "uname", return_value=SimpleNamespace(sysname="Darwin")), \
                patch("deploy.starter_host.prerequisites") as prerequisites, \
                patch.object(acceptance, "exercise", return_value={"https_and_auth": True}) as exercise, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            acceptance.main()
        prerequisites.assert_called_once_with(True)
        self.assertEqual(exercise.call_args.args[1], stores)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["external_stores"], {"ownership": "caller", "preserved": True, "disposable_acknowledged": True})
        for secret in (*stores.values(), "db-secret", "cache-secret", "operator-password"):
            self.assertNotIn(secret, self.receipt.read_text() + output.getvalue())


if __name__ == "__main__":
    unittest.main()

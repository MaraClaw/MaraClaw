from dataclasses import dataclass
from pathlib import Path
from stat import S_IMODE
from uuid import uuid4

from app.services import gogcli_runtime
from app.services.gogcli_runtime import (
    GOG_HOME,
    GOG_KEYRING_BACKEND,
    GOGCLI_SECRET_MOUNT_PATH,
    gogcli_docker_extras,
    gogcli_secret_file,
    gogcli_skill_folder_names,
    write_gogcli_keyring_secret,
)


@dataclass(frozen=True, slots=True)
class FakeAgent:
    id: str
    gogcli_enabled: bool


def configure_runtime(monkeypatch, local_root: Path, *, enabled: bool) -> None:
    monkeypatch.setattr(gogcli_runtime, "GOGCLI_ENABLED", enabled, raising=False)
    monkeypatch.setattr(gogcli_runtime.settings, "GOGCLI_ENABLED", enabled, raising=False)
    monkeypatch.setattr(gogcli_runtime.settings, "STORAGE_LOCAL_ROOT", str(local_root))
    monkeypatch.setattr(gogcli_runtime.settings, "AGENT_DATA_DIR", str(local_root / "agent-data"))


def test_docker_extras_are_empty_when_global_flag_is_disabled(monkeypatch, tmp_path: Path) -> None:
    # Given
    local_root = tmp_path / "backend-local"
    agent_dir = tmp_path / "agent-workspace"
    agent = FakeAgent(id=str(uuid4()), gogcli_enabled=True)
    configure_runtime(monkeypatch, local_root, enabled=False)

    # When
    env, volumes = gogcli_docker_extras(agent, agent_dir)

    # Then
    assert env == {}
    assert volumes == []
    assert not agent_dir.exists()
    assert not local_root.exists()


def test_docker_extras_are_empty_when_agent_flag_is_disabled(monkeypatch, tmp_path: Path) -> None:
    # Given
    local_root = tmp_path / "backend-local"
    agent_dir = tmp_path / "agent-workspace"
    agent = FakeAgent(id=str(uuid4()), gogcli_enabled=False)
    configure_runtime(monkeypatch, local_root, enabled=True)

    # When
    env, volumes = gogcli_docker_extras(agent, agent_dir)

    # Then
    assert env == {}
    assert volumes == []


def test_docker_extras_use_file_keyring_without_secret(monkeypatch, tmp_path: Path) -> None:
    # Given
    local_root = tmp_path / "backend-local"
    agent_dir = tmp_path / "agent-workspace"
    agent = FakeAgent(id=str(uuid4()), gogcli_enabled=True)
    configure_runtime(monkeypatch, local_root, enabled=True)

    # When
    env, volumes = gogcli_docker_extras(agent, agent_dir)

    # Then
    assert GOG_HOME == "/home/node/.openclaw/gogcli"
    assert GOG_KEYRING_BACKEND == "file"
    assert env == {
        "GOG_HOME": GOG_HOME,
        "GOG_KEYRING_BACKEND": GOG_KEYRING_BACKEND,
    }
    assert "GOG_KEYRING_PASSWORD" not in env
    assert "GOG_KEYRING_PASSWORD_FILE" not in env
    assert volumes == []


def test_docker_extras_mount_existing_secret_read_only(monkeypatch, tmp_path: Path) -> None:
    # Given
    local_root = tmp_path / "backend-local"
    agent_dir = tmp_path / "agent-workspace"
    agent = FakeAgent(id=str(uuid4()), gogcli_enabled=True)
    configure_runtime(monkeypatch, local_root, enabled=True)
    secret_file = gogcli_secret_file(agent.id)
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("correct horse battery staple", encoding="utf-8")

    # When
    env, volumes = gogcli_docker_extras(agent, agent_dir)

    # Then
    assert env["GOG_KEYRING_PASSWORD_FILE"] == GOGCLI_SECRET_MOUNT_PATH
    assert "GOG_KEYRING_PASSWORD" not in env
    assert volumes == [(str(secret_file), GOGCLI_SECRET_MOUNT_PATH, "ro")]
    assert GOGCLI_SECRET_MOUNT_PATH == "/run/secrets/gogcli_keyring_password"
    assert all(container_path != "/run/secrets" for _, container_path, _ in volumes)
    assert secret_file.is_relative_to(local_root)
    assert not secret_file.is_relative_to(agent_dir)


def test_write_keyring_secret_uses_backend_local_secret_root(monkeypatch, tmp_path: Path) -> None:
    # Given
    local_root = tmp_path / "backend-local"
    agent_dir = tmp_path / "agent-workspace"
    agent_id = str(uuid4())
    configure_runtime(monkeypatch, local_root, enabled=True)

    # When
    written_file = write_gogcli_keyring_secret(agent_id, "correct horse battery staple")

    # Then
    assert written_file == gogcli_secret_file(agent_id)
    assert written_file.read_text(encoding="utf-8") == "correct horse battery staple"
    assert written_file.is_relative_to(local_root)
    assert not written_file.is_relative_to(agent_dir)
    assert written_file.parent.name == agent_id
    assert S_IMODE(written_file.parent.stat().st_mode) == 0o700
    assert S_IMODE(written_file.stat().st_mode) == 0o600


def test_extract_auth_url_returns_only_oauth_consent_url() -> None:
    # Given
    stdout = (
        "gogcli login started\n"
        "Visit https://accounts.google.com/o/oauth2/v2/auth?client_id=public-client&scope=email to continue.\n"
        "raw diagnostic line: should-not-leak\n"
    )
    extract_auth_url = gogcli_runtime.extract_gogcli_auth_url

    # When
    auth_url = extract_auth_url(stdout)

    # Then
    assert auth_url == "https://accounts.google.com/o/oauth2/v2/auth?client_id=public-client&scope=email"
    assert "should-not-leak" not in auth_url


def test_parse_auth_status_sanitizes_raw_gog_output() -> None:
    # Given
    stdout = "Logged in as maraclaw@example.com\nraw diagnostic line: should-not-leak\n"
    stderr = "debug trace: should-not-leak"
    parse_auth_status = gogcli_runtime.parse_gogcli_auth_status

    # When
    status = parse_auth_status(stdout=stdout, stderr=stderr, exit_code=0)

    # Then
    assert status.authenticated is True
    assert status.account_hint == "maraclaw@example.com"
    assert status.detail == "Authenticated"
    serialized = status.model_dump()
    assert "stdout" not in serialized
    assert "stderr" not in serialized
    assert "should-not-leak" not in repr(serialized)


def test_parse_auth_status_handles_unauthenticated_output_safely() -> None:
    # Given
    stdout = "not logged in; run gog auth login"
    stderr = "debug trace: should-not-leak"
    parse_auth_status = gogcli_runtime.parse_gogcli_auth_status

    # When
    status = parse_auth_status(stdout=stdout, stderr=stderr, exit_code=1)

    # Then
    assert status.authenticated is False
    assert status.account_hint is None
    assert status.detail == "Not authenticated"
    assert "should-not-leak" not in repr(status.model_dump())


def test_skill_folder_names_include_all_vendored_upstream_skills(monkeypatch, tmp_path: Path) -> None:
    # Given
    configure_runtime(monkeypatch, tmp_path / "backend-local", enabled=True)

    # When
    folder_names = set(gogcli_skill_folder_names())

    # Then
    assert len(folder_names) == 31
    assert {
        "crabbox",
        "gog",
        "gog-admin",
        "gog-calendar",
        "gog-drive-audit",
        "gog-gmail",
        "gog-sites",
        "gog-weekly-digest",
        "gog-youtube",
    }.issubset(folder_names)


def test_gog_sheets_skill_documents_core_commands() -> None:
    # Given
    skill_file = Path(__file__).resolve().parents[1] / "app/services/gogcli_skill_files/gog-sheets/SKILL.md"

    # When
    skill_text = skill_file.read_text(encoding="utf-8")

    # Then
    assert "<!-- Generated by scripts/gen-agent-skills.mjs; do not edit. -->" in skill_text
    assert "| `filter` | Manage basic filters |" in skill_text
    assert "| `conditional-format` | Manage conditional formatting rules |" in skill_text
    assert "gog schema sheets" in skill_text


def test_gog_gmail_and_drive_skills_document_v0350_commands() -> None:
    # Given
    root = Path(__file__).resolve().parents[1] / "app/services/gogcli_skill_files"
    gmail_text = (root / "gog-gmail" / "SKILL.md").read_text(encoding="utf-8")
    drive_text = (root / "gog-drive" / "SKILL.md").read_text(encoding="utf-8")

    # Then - commands added in the gogcli 0.34.x/0.35.0 skill surface
    assert "| `import` | Import an RFC822/EML message into Gmail |" in gmail_text
    assert "| `sync` | Reconcile local files with Drive |" in drive_text

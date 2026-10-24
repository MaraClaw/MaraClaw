import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import company_transfer_credentials as credentials


@pytest.mark.parametrize("free", [True, False])
def test_credential_scrub_preserves_workspace_and_guest_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free: bool
) -> None:
    # Given source credentials beside retained files, memory and a mixed guest database.
    root = tmp_path / str(uuid4())
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("retained memory", encoding="utf-8")
    (workspace / "report.txt").write_text("retained file", encoding="utf-8")
    guest = root / "agents/main/agent"
    guest.mkdir(parents=True)
    (guest / "auth-profiles.json").write_text('{"secret":"source"}', encoding="utf-8")
    database = guest / "openclaw-agent.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE auth_profile_store(secret TEXT)")
        db.execute("INSERT INTO auth_profile_store VALUES ('source')")
        db.execute("CREATE TABLE messages(content TEXT)")
        db.execute("INSERT INTO messages VALUES ('retained history')")
    config = {
        "env": {"SOURCE_KEY": "secret"},
        "models": {"source": "key"},
        "auth": {"source": "secret"},
        "mcp": {"servers": {"source-pool": {"token": "secret"}, "personal": {"token": "user-owned"}}},
    }
    (root / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    secret = tmp_path / "_gogcli_secrets" / root.name / "keyring_password"
    secret.parent.mkdir(parents=True)
    secret.write_text("personal-secret", encoding="utf-8")
    monkeypatch.setattr(credentials, "gogcli_secret_file", lambda _id: secret)
    # When only transfer credential artifacts are scrubbed.
    credentials.scrub_credentials(root, free=free, source_tool_names=frozenset({"source-pool"}))
    # Then memory/files/chat survive; source model credentials do not, and paid personal integrations survive.
    assert (workspace / "MEMORY.md").read_text() == "retained memory"
    assert (workspace / "report.txt").read_text() == "retained file"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM messages").fetchall() == [("retained history",)]
        assert db.execute("SELECT * FROM auth_profile_store").fetchall() == []
    assert not (guest / "auth-profiles.json").exists()
    if free:
        assert not (root / "openclaw.json").exists()
        assert not secret.exists()
    else:
        cleaned = json.loads((root / "openclaw.json").read_text())
        assert cleaned == {"mcp": {"servers": {"personal": {"token": "user-owned"}}}}
        assert secret.read_text() == "personal-secret"


def test_credential_cleanup_refuses_symlinked_parent(tmp_path: Path) -> None:
    # Given guest storage reached through a symlink to someone else's directory.
    real = tmp_path / "real"
    real.mkdir()
    (real / "openclaw.json").write_text('{"secret":"untouched"}', encoding="utf-8")
    linked = tmp_path / "link"
    linked.symlink_to(real, target_is_directory=True)
    # When credential cleanup encounters that path.
    with pytest.raises(OSError, match="symlinked"):
        credentials.scrub_credentials(linked, free=True, source_tool_names=frozenset())
    # Then the outside credential has not been touched.
    assert (real / "openclaw.json").read_text() == '{"secret":"untouched"}'

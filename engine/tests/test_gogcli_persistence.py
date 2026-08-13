import base64
import io
import shutil
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import gogcli_persistence, gogcli_runtime
from app.services.gogcli_runtime import GogcliAuthStatus


class FakeGogcliDAO:
    """In-memory stand-in for gogcli_credential_state_dao."""

    def __init__(self, state=None) -> None:
        self.state = state

    async def get_by_agent(self, agent_id):
        if self.state and self.state.agent_id == agent_id:
            return self.state
        return None

    async def upsert_fields(self, agent_id, fields):
        if self.state is None:
            self.state = SimpleNamespace(agent_id=agent_id, status="unauthenticated")
        for key, value in fields.items():
            setattr(self.state, key, value)
        return self.state

    async def update(self, *, db_obj, obj_in):
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        self.state = db_obj
        return db_obj


def configure_runtime(monkeypatch, local_root: Path) -> None:
    monkeypatch.setattr(gogcli_runtime.settings, "STORAGE_LOCAL_ROOT", str(local_root))
    monkeypatch.setattr(gogcli_runtime.settings, "AGENT_DATA_DIR", str(local_root / "agent-data"))


def bind_dao(monkeypatch: pytest.MonkeyPatch, dao: FakeGogcliDAO) -> None:
    monkeypatch.setattr(gogcli_persistence, "gogcli_credential_state_dao", dao)


@pytest.mark.asyncio
async def test_keyring_password_and_authenticated_data_restore_from_encrypted_state(
    monkeypatch, tmp_path: Path
) -> None:
    # Given
    configure_runtime(monkeypatch, tmp_path / "backend-local")
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / "agent-workspace"
    data_dir = agent_dir / "gogcli" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "credentials.json").write_text('{"token":"raw-token-stays-inside-encrypted-archive"}', encoding="utf-8")
    dao = FakeGogcliDAO()
    bind_dao(monkeypatch, dao)
    status = GogcliAuthStatus(authenticated=True, account_hint="maraclaw@example.com", detail="Authenticated")

    # When
    await gogcli_persistence.upsert_gogcli_keyring_password(None, agent_id, "correct horse battery staple")
    await gogcli_persistence.capture_authenticated_gogcli_state(None, agent_id, agent_dir, status)
    shutil.rmtree(data_dir)
    await gogcli_persistence.restore_gogcli_state(None, agent_id, agent_dir)

    # Then
    assert dao.state is not None
    assert dao.state.status == "authenticated"
    assert dao.state.account_hint == "maraclaw@example.com"
    encrypted_keyring_password = dao.state.encrypted_keyring_password
    encrypted_gog_data_archive = dao.state.encrypted_gog_data_archive
    assert encrypted_keyring_password is not None
    assert encrypted_gog_data_archive is not None
    assert "correct horse battery staple" not in encrypted_keyring_password
    assert "raw-token-stays-inside-encrypted-archive" not in encrypted_gog_data_archive
    assert gogcli_runtime.gogcli_secret_file(agent_id).read_text(encoding="utf-8") == "correct horse battery staple"
    assert (data_dir / "credentials.json").read_text(
        encoding="utf-8"
    ) == '{"token":"raw-token-stays-inside-encrypted-archive"}'
    assert dao.state.last_restored_at is not None


@pytest.mark.asyncio
async def test_capture_authenticated_state_skips_when_status_is_not_authenticated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / "agent-workspace"
    (agent_dir / "gogcli" / "data").mkdir(parents=True)
    dao = FakeGogcliDAO()
    bind_dao(monkeypatch, dao)
    status = GogcliAuthStatus(authenticated=False, account_hint=None, detail="Not authenticated")

    # When
    await gogcli_persistence.capture_authenticated_gogcli_state(None, agent_id, agent_dir, status)

    # Then
    assert dao.state is None


@pytest.mark.asyncio
async def test_mark_needs_reauth_updates_only_when_snapshot_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    agent_id = uuid.uuid4()
    state = SimpleNamespace(
        agent_id=agent_id,
        encrypted_gog_data_archive="encrypted-archive",
        status="authenticated",
        last_status_checked_at=None,
    )
    dao = FakeGogcliDAO(state)
    bind_dao(monkeypatch, dao)

    # When
    await gogcli_persistence.mark_gogcli_needs_reauth_if_snapshot_exists(None, agent_id)

    # Then
    assert state.status == "needs_reauth"
    assert state.last_status_checked_at is not None


@pytest.mark.asyncio
async def test_restore_marks_needs_reauth_when_encrypted_state_is_corrupted(monkeypatch, tmp_path: Path) -> None:
    # Given
    configure_runtime(monkeypatch, tmp_path / "backend-local")
    agent_id = uuid.uuid4()
    state = SimpleNamespace(
        agent_id=agent_id,
        encrypted_gog_data_archive="corrupted",
        encrypted_keyring_password=None,
        status="authenticated",
        last_status_checked_at=None,
    )
    dao = FakeGogcliDAO(state)
    bind_dao(monkeypatch, dao)

    # When
    restored = await gogcli_persistence.restore_gogcli_state(None, agent_id, tmp_path / "agent-workspace")

    # Then
    assert restored is False
    assert state.status == "needs_reauth"
    assert state.last_status_checked_at is not None
    assert not gogcli_runtime.gogcli_secret_file(agent_id).exists()


def test_restore_archive_rejects_path_traversal(tmp_path: Path) -> None:
    # Given
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../escape.txt", "escaped")
    archive_b64 = base64.b64encode(archive_buffer.getvalue()).decode("ascii")

    # When / Then
    with pytest.raises(gogcli_persistence.GogcliArchivePathError):
        gogcli_persistence.restore_gogcli_data_archive(tmp_path / "agent", archive_b64)
    assert not (tmp_path / "escape.txt").exists()


def test_build_archive_skips_symlinked_file_targets(tmp_path: Path) -> None:
    # Given
    agent_dir = tmp_path / "agent"
    data_dir = agent_dir / "gogcli" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "credentials.json").write_text("safe-credential-state", encoding="utf-8")
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("HOST_SECRET_MARKER", encoding="utf-8")
    (data_dir / "linked-secret").symlink_to(outside_secret)

    # When
    archive_b64 = gogcli_persistence.build_gogcli_data_archive(agent_dir)

    # Then
    assert archive_b64 is not None
    archive_bytes = base64.b64decode(archive_b64, validate=True)
    with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
        names = archive.namelist()
    assert "credentials.json" in names
    assert "linked-secret" not in names
    assert b"HOST_SECRET_MARKER" not in archive_bytes

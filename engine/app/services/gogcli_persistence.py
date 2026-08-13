"""Encrypted DB persistence for gogcli file-keyring state."""

import base64
import binascii
import io
import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override
from uuid import UUID

from anyio.to_thread import run_sync

from app.config import get_settings
from app.core.logging import logger
from app.core.security import decrypt_data, encrypt_data
from app.dao.gogcli_credential_dao import gogcli_credential_state_dao
from app.records.gogcli_credential import GogcliCredentialStateRecord
from app.services.gogcli_runtime import GogcliAuthStatus, write_gogcli_keyring_secret

settings = get_settings()


@dataclass(frozen=True, slots=True)
class GogcliArchivePathError(Exception):
    """Raised when a gogcli archive member would escape the data directory."""

    member_name: str

    @override
    def __str__(self) -> str:
        return f"gogcli archive member escapes data directory: {self.member_name}"


def _path_is_within(root: Path, candidate: Path) -> bool:
    return os.path.commonpath([str(root), str(candidate)]) == str(root)


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise GogcliArchivePathError(path.as_posix())


def _gogcli_data_dir(agent_dir: Path) -> Path:
    _reject_symlink(agent_dir)
    gogcli_dir = agent_dir / "gogcli"
    _reject_symlink(gogcli_dir)
    data_dir = gogcli_dir / "data"
    _reject_symlink(data_dir)
    agent_root = agent_dir.resolve()
    data_root = data_dir.resolve()
    if not _path_is_within(agent_root, data_root):
        raise GogcliArchivePathError(data_dir.as_posix())
    return data_dir


async def get_gogcli_credential_state(db: Any, agent_id: UUID) -> GogcliCredentialStateRecord | None:
    """Return the persisted gogcli credential row for one agent."""
    return await gogcli_credential_state_dao.get_by_agent(agent_id)


async def upsert_gogcli_keyring_password(db: Any, agent_id: UUID, password: str) -> GogcliCredentialStateRecord:
    """Persist an encrypted gogcli file-keyring password for one agent."""
    now = datetime.now(UTC)
    encrypted_password = encrypt_data(password, settings.SECRET_KEY)
    existing = await gogcli_credential_state_dao.get_by_agent(agent_id)
    fields: dict[str, Any] = {
        "encrypted_keyring_password": encrypted_password,
        "keyring_password_updated_at": now,
    }
    if existing is None:
        fields["status"] = "unauthenticated"
    return await gogcli_credential_state_dao.upsert_fields(agent_id, fields)


async def restore_gogcli_state(db: Any, agent_id: UUID, agent_dir: Path) -> bool:
    """Restore encrypted gogcli keyring and data snapshot into a materialized agent directory."""
    state = await gogcli_credential_state_dao.get_by_agent(agent_id)
    if state is None:
        return False

    restored = False
    try:
        if state.encrypted_keyring_password:
            password = decrypt_data(state.encrypted_keyring_password, settings.SECRET_KEY)
            await run_sync(write_gogcli_keyring_secret, agent_id, password)
            restored = True

        if state.encrypted_gog_data_archive:
            archive_b64 = decrypt_data(state.encrypted_gog_data_archive, settings.SECRET_KEY)
            await run_sync(restore_gogcli_data_archive, agent_dir, archive_b64)
            restored = True
    except (ValueError, binascii.Error, zipfile.BadZipFile, OSError, GogcliArchivePathError) as error:
        await gogcli_credential_state_dao.update(
            db_obj=state,
            obj_in={"status": "needs_reauth", "last_status_checked_at": datetime.now(UTC)},
        )
        logger.warning(f"gogcli state restore failed for agent {agent_id}: {type(error).__name__}")
        return False

    if restored:
        await gogcli_credential_state_dao.update(
            db_obj=state,
            obj_in={"last_restored_at": datetime.now(UTC)},
        )
    return restored


async def capture_authenticated_gogcli_state(
    db: Any,
    agent_id: UUID,
    agent_dir: Path,
    status: GogcliAuthStatus,
) -> GogcliCredentialStateRecord | None:
    """Persist an encrypted gogcli data snapshot when gogcli reports an authenticated account."""
    if not status.authenticated:
        return None

    try:
        archive_b64 = await run_sync(build_gogcli_data_archive, agent_dir)
    except GogcliArchivePathError as error:
        logger.warning(f"gogcli data archive skipped for agent {agent_id}: {type(error).__name__}")
        return None
    if archive_b64 is None:
        return None

    now = datetime.now(UTC)
    return await gogcli_credential_state_dao.upsert_fields(
        agent_id,
        {
            "encrypted_gog_data_archive": encrypt_data(archive_b64, settings.SECRET_KEY),
            "account_hint": status.account_hint,
            "status": "authenticated",
            "credential_snapshot_updated_at": now,
            "last_authenticated_at": now,
            "last_status_checked_at": now,
        },
    )


async def mark_gogcli_needs_reauth_if_snapshot_exists(db: Any, agent_id: UUID) -> bool:
    """Mark a previously snapshotted gogcli state as requiring re-authentication."""
    state = await gogcli_credential_state_dao.get_by_agent(agent_id)
    if state is None or not state.encrypted_gog_data_archive:
        return False
    await gogcli_credential_state_dao.update(
        db_obj=state,
        obj_in={"status": "needs_reauth", "last_status_checked_at": datetime.now(UTC)},
    )
    return True


def build_gogcli_data_archive(agent_dir: Path) -> str | None:
    """Build a base64 zip archive of the local gogcli data directory."""
    data_dir = _gogcli_data_dir(agent_dir)
    if not data_dir.is_dir():
        return None
    data_root = data_dir.resolve()
    files = []
    for path in sorted(data_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if not _path_is_within(data_root, path.resolve()):
            raise GogcliArchivePathError(path.relative_to(data_dir).as_posix())
        files.append(path)
    if not files:
        return None

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(data_dir).as_posix())
    return base64.b64encode(archive_buffer.getvalue()).decode("ascii")


def restore_gogcli_data_archive(agent_dir: Path, archive_b64: str) -> None:
    """Restore a base64 gogcli data archive after rejecting path traversal."""
    data_dir = _gogcli_data_dir(agent_dir)
    archive_bytes = base64.b64decode(archive_b64, validate=True)
    with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
        _validate_archive_members(data_dir, archive.infolist())
        if data_dir.exists():
            shutil.rmtree(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        for member in archive.infolist():
            if member.is_dir():
                (data_dir / member.filename).mkdir(parents=True, exist_ok=True)
                continue
            target = data_dir / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)


def _validate_archive_members(data_dir: Path, members: list[zipfile.ZipInfo]) -> None:
    data_root = data_dir.resolve()
    for member in members:
        target = (data_dir / member.filename).resolve()
        if not _path_is_within(data_root, target):
            raise GogcliArchivePathError(member.filename)

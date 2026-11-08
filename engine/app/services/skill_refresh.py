from typing import final
from uuid import UUID

import anyio
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.dao.skill_dao import skill_dao
from app.db.session import connection_ctx, get_connection
from app.records.skill import SkillSource
from app.records.user import UserRecord
from app.services.skill_access import ensure_skill_write_access, fresh_import_actor
from app.services.skill_content import sanitized_skill_files, skill_fingerprint


@final
class SkillRefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._+-]+$")


class SkillRefreshResponse(BaseModel):
    id: UUID
    name: str
    folder_name: str
    source_metadata: SkillSource
    imported_fingerprint: str


async def refresh_skill(
    skill_id: UUID, body: SkillRefreshIn, current_user: UserRecord, *, api_key: str = ""
) -> SkillRefreshResponse:
    from app.services.skill_archive import fetch_clawhub_skill_archive

    if get_connection() is not None:
        raise RuntimeError("Skill refresh must own its transaction")
    skill = await skill_dao.get_for_tenant_scope(skill_id, tenant_id=current_user.tenant_id, role=current_user.role)
    if skill is None:
        raise HTTPException(404, "Skill not found")
    ensure_skill_write_access(skill, current_user)
    source = skill.source_metadata
    if source is None:
        raise HTTPException(409, "Skill has no tracked ClawHub import")
    files, archive_source = await fetch_clawhub_skill_archive(source.slug, api_key=api_key, version=body.version)
    files = sanitized_skill_files(files)
    next_source = source.model_copy(update={"requested_version": body.version})
    next_source = SkillSource.model_validate({**next_source.model_dump(), "archive_source": archive_source})
    fingerprint = skill_fingerprint((file["path"], file["content"]) for file in files)
    result = SkillRefreshResponse(
        id=skill.id,
        name=skill.name,
        folder_name=skill.folder_name,
        source_metadata=next_source,
        imported_fingerprint=fingerprint,
    )
    committing = False
    try:
        async with connection_ctx():
            locked = await skill_dao.get_for_tenant_scope(
                skill_id, tenant_id=current_user.tenant_id, role=current_user.role, with_files=True, for_update=True
            )
            actor = await fresh_import_actor(current_user)
            if actor.role not in ("platform_admin", "org_admin"):
                raise HTTPException(403, "Admin role required")
            if locked is None:
                raise HTTPException(404, "Skill not found")
            ensure_skill_write_access(locked, actor)
            if (
                locked.source_metadata != source
                or locked.imported_fingerprint != body.expected_fingerprint
                or skill_fingerprint((file.path, file.content) for file in locked.files) != body.expected_fingerprint
            ):
                raise HTTPException(409, "Skill changed since import; reload before refreshing")
            updated = await skill_dao.replace_imported_package(locked, files, next_source)
            result = SkillRefreshResponse(
                id=updated.id,
                name=updated.name,
                folder_name=updated.folder_name,
                source_metadata=next_source,
                imported_fingerprint=fingerprint,
            )
            await anyio.lowlevel.checkpoint()
            committing = True
    except anyio.get_cancelled_exc_class():
        if not committing:
            raise
        # COMMIT may have succeeded despite cancellation; only a fresh read can settle its outcome.
        with anyio.CancelScope(shield=True):
            async with connection_ctx():
                persisted = await skill_dao.get_for_tenant_scope(
                    skill_id, tenant_id=current_user.tenant_id, role=current_user.role, with_files=True, for_update=True
                )
                persisted_fingerprint = (
                    skill_fingerprint((file.path, file.content) for file in persisted.files)
                    if persisted is not None
                    else None
                )
                if (
                    persisted is not None
                    and persisted.source_metadata == next_source
                    and persisted.imported_fingerprint == fingerprint
                    and persisted_fingerprint == fingerprint
                ):
                    return result
                if (
                    persisted is None
                    or persisted.source_metadata != source
                    or persisted.imported_fingerprint != body.expected_fingerprint
                    or persisted_fingerprint != body.expected_fingerprint
                ):
                    raise HTTPException(409, "Refresh outcome changed; reload the skill") from None
        raise
    return result

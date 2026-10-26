"""Deterministic folder selection for a creation snapshot."""

from collections.abc import Sequence
from uuid import UUID

from fastapi import HTTPException

from app.dao.provisioning_skill_dao import provisioning_skills
from app.records.skill import SkillRecord


async def resolve_setup_skills(
    tenant_id: UUID | None, explicit_ids: Sequence[UUID], folders: Sequence[str]
) -> list[SkillRecord]:
    candidates = await provisioning_skills(tenant_id, explicit_ids, folders)
    explicit = set(explicit_ids)
    if explicit - {skill.id for skill in candidates}:
        raise HTTPException(422, detail={"code": "agent_setup_skill_unavailable"})
    selected: dict[str, SkillRecord] = {}
    explicit_folders: set[str] = set()
    for skill in candidates:
        if skill.id in explicit:
            if skill.folder_name in explicit_folders:
                raise HTTPException(422, detail={"code": "agent_setup_skill_folder_conflict"})
            explicit_folders.add(skill.folder_name)
        else:
            selected[skill.folder_name] = skill
    for skill in candidates:
        if skill.id in explicit:
            selected[skill.folder_name] = skill
    return [selected[folder] for folder in sorted(selected)]

"""Destination-scoped skill snapshots, independent of catalog browsing permissions."""

from collections.abc import Sequence
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.db.session import connection_ctx
from app.records.skill import SkillFileRecord, SkillRecord


async def provisioning_skills(
    tenant_id: UUID | None, explicit_ids: Sequence[UUID], folders: Sequence[str]
) -> list[SkillRecord]:
    params = {"tenant": tenant_id, "ids": list(explicit_ids), "folders": list(folders)}
    async with connection_ctx() as db:
        rows = await db.fetchall(
            """SELECT * FROM skills
            WHERE (tenant_id IS NULL OR tenant_id = %(tenant)s)
              AND (id = ANY(%(ids)s::uuid[]) OR is_default OR folder_name = ANY(%(folders)s::text[]))
            ORDER BY folder_name, (tenant_id IS NOT NULL), id FOR SHARE""",
            params,
        )
        file_rows = await db.fetchall(
            """SELECT f.* FROM skill_files f JOIN skills s ON s.id = f.skill_id
            WHERE s.id = ANY(%(ids)s::uuid[]) AND (s.tenant_id IS NULL OR s.tenant_id = %(tenant)s)
            ORDER BY f.path, f.id""",
            {"ids": [row["id"] for row in rows], "tenant": tenant_id},
        )
        files: dict[UUID, list[SkillFileRecord]] = {}
        for row in file_rows:
            record = SkillFileRecord.from_row(row)
            files.setdefault(record.skill_id, []).append(record)
        return [SkillRecord.from_row(row, files=files.get(uuid_from_row(row["id"]), [])) for row in rows]

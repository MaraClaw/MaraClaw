"""DAO for skills and skill_files (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.skill import SkillFileRecord, SkillRecord

_SKILL_COLUMNS = (
    "id",
    "tenant_id",
    "name",
    "description",
    "category",
    "icon",
    "folder_name",
    "is_builtin",
    "is_default",
    "created_at",
)

_SKILL_FILE_COLUMNS = ("id", "skill_id", "path", "content")


class SkillDAO(BaseDAO[SkillRecord]):
    """DAO for global skill registry rows."""

    table = "skills"
    columns = _SKILL_COLUMNS
    record_factory = staticmethod(lambda row: SkillRecord.from_row(row))

    async def get_by_folder_name(
        self,
        folder_name: str,
        *,
        tenant_id: UUID | None = None,
        tenant_scoped: bool = False,
    ) -> SkillRecord | None:
        params: dict = {"folder_name": folder_name}
        tenant_sql = ""
        if tenant_scoped:
            if tenant_id is not None:
                tenant_sql = " AND tenant_id = %(tenant_id)s"
                params["tenant_id"] = tenant_id
            else:
                tenant_sql = " AND tenant_id IS NULL"
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM skills WHERE folder_name = %(folder_name)s{tenant_sql} LIMIT 1",
                params,
            )
            return SkillRecord.from_row(row) if row else None

    async def list_for_tenant_scope(self, tenant_id: UUID | None) -> Sequence[SkillRecord]:
        async with self.session() as db:
            if tenant_id is not None:
                rows = await db.fetchall(
                    f"SELECT {self._select_list()} FROM skills "
                    "WHERE tenant_id IS NULL OR tenant_id = %(tenant_id)s ORDER BY name",
                    {"tenant_id": tenant_id},
                )
            else:
                rows = await db.fetchall(f"SELECT {self._select_list()} FROM skills ORDER BY name")
            return [SkillRecord.from_row(row) for row in rows]

    async def get_for_tenant_scope(
        self,
        skill_id: UUID | str,
        *,
        tenant_id: UUID | None,
        role: str | None = None,
        with_files: bool = False,
    ) -> SkillRecord | None:
        params: dict = {"skill_id": skill_id}
        scope_sql = ""
        if role != "platform_admin" and tenant_id is not None:
            scope_sql = " AND (tenant_id IS NULL OR tenant_id = %(tenant_id)s)"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM skills WHERE id = %(skill_id)s{scope_sql} LIMIT 1",
                params,
            )
            if not row:
                return None
            files = list(await skill_file_dao.list_for_skill(row["id"])) if with_files else []
            return SkillRecord.from_row(row, files=files)

    async def get_by_folder_for_tenant_scope(
        self,
        folder_name: str,
        *,
        tenant_id: UUID | None,
        role: str | None = None,
        with_files: bool = False,
    ) -> SkillRecord | None:
        params: dict = {"folder_name": folder_name}
        scope_sql = ""
        if role != "platform_admin" and tenant_id is not None:
            scope_sql = " AND (tenant_id IS NULL OR tenant_id = %(tenant_id)s)"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM skills WHERE folder_name = %(folder_name)s{scope_sql} LIMIT 1",
                params,
            )
            if not row:
                return None
            files = list(await skill_file_dao.list_for_skill(row["id"])) if with_files else []
            return SkillRecord.from_row(row, files=files)

    async def replace_files(self, skill_id: UUID, files: Sequence[tuple[str, str]]) -> None:
        async with self.session() as db:
            await db.execute("DELETE FROM skill_files WHERE skill_id = %(skill_id)s", {"skill_id": skill_id})
        for path, content in files:
            await skill_file_dao.create(obj_in={"skill_id": skill_id, "path": path, "content": content})

    async def delete_with_files(self, skill_id: UUID) -> None:
        async with self.session() as db:
            await db.execute("DELETE FROM skill_files WHERE skill_id = %(skill_id)s", {"skill_id": skill_id})
            await db.execute("DELETE FROM skills WHERE id = %(skill_id)s", {"skill_id": skill_id})

    async def list_defaults_with_files(self) -> Sequence[SkillRecord]:
        return await self._list_with_files("is_default IS TRUE")

    async def list_all_with_files(self) -> Sequence[SkillRecord]:
        return await self._list_with_files("TRUE")

    async def _list_with_files(self, where_sql: str) -> Sequence[SkillRecord]:
        async with self.session() as db:
            skill_rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM skills WHERE {where_sql} ORDER BY folder_name"
            )
            if not skill_rows:
                return []
            skill_ids = [row["id"] for row in skill_rows]
            file_rows = await db.fetchall(
                f"SELECT {skill_file_dao._select_list()} FROM skill_files "
                "WHERE skill_id = ANY(%(skill_ids)s) ORDER BY path",
                {"skill_ids": skill_ids},
            )
            files_by_skill: dict[UUID, list[SkillFileRecord]] = {}
            for frow in file_rows:
                rec = SkillFileRecord.from_row(frow)
                files_by_skill.setdefault(rec.skill_id, []).append(rec)
            return [SkillRecord.from_row(row, files=files_by_skill.get(row["id"], [])) for row in skill_rows]

    async def list_default_ids(self) -> set[UUID]:
        async with self.session() as db:
            rows = await db.fetchall("SELECT id FROM skills WHERE is_default IS TRUE")
            return {row["id"] for row in rows}

    async def list_ids_by_folder_names(self, folder_names: Sequence[str]) -> set[UUID]:
        if not folder_names:
            return set()
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM skills WHERE folder_name = ANY(%(names)s)",
                {"names": list(folder_names)},
            )
            return {row["id"] for row in rows}

    async def list_with_files_by_ids(self, skill_ids: Sequence[UUID]) -> Sequence[SkillRecord]:
        if not skill_ids:
            return []
        async with self.session() as db:
            skill_rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM skills WHERE id = ANY(%(ids)s)",
                {"ids": list(skill_ids)},
            )
            if not skill_rows:
                return []
            file_rows = await db.fetchall(
                f"SELECT {skill_file_dao._select_list()} FROM skill_files "
                "WHERE skill_id = ANY(%(skill_ids)s) ORDER BY path",
                {"skill_ids": [row["id"] for row in skill_rows]},
            )
            files_by_skill: dict[UUID, list[SkillFileRecord]] = {}
            for frow in file_rows:
                rec = SkillFileRecord.from_row(frow)
                files_by_skill.setdefault(rec.skill_id, []).append(rec)
            return [SkillRecord.from_row(row, files=files_by_skill.get(row["id"], [])) for row in skill_rows]

    async def list_files(self, skill_id: UUID) -> Sequence[SkillFileRecord]:
        return await skill_file_dao.list_for_skill(skill_id)

    async def upsert_skill_package(
        self,
        *,
        name: str,
        description: str,
        category: str,
        icon: str,
        folder_name: str,
        is_builtin: bool,
        is_default: bool,
        files: Sequence[tuple[str, str]],
        drop_missing_files: bool = False,
    ) -> SkillRecord:
        """Idempotently upsert a skill and its files (seeder helper)."""
        existing = await self.get_by_folder_name(folder_name)
        if existing is None:
            skill = await self.create(
                obj_in={
                    "name": name,
                    "description": description,
                    "category": category,
                    "icon": icon,
                    "folder_name": folder_name,
                    "is_builtin": is_builtin,
                    "is_default": is_default,
                }
            )
            for path, content in files:
                await skill_file_dao.create(
                    obj_in={"skill_id": skill.id, "path": path, "content": content},
                )
            skill.files = list(await skill_file_dao.list_for_skill(skill.id))
            return skill

        skill = await self.update(
            db_obj=existing,
            obj_in={
                "name": name,
                "description": description,
                "category": category,
                "icon": icon,
                "is_builtin": is_builtin,
                "is_default": is_default,
            },
        )
        existing_files = {f.path: f for f in await skill_file_dao.list_for_skill(skill.id)}
        seen: set[str] = set()
        for path, content in files:
            seen.add(path)
            existing_file = existing_files.get(path)
            if existing_file is None:
                await skill_file_dao.create(obj_in={"skill_id": skill.id, "path": path, "content": content})
            elif existing_file.content != content:
                await skill_file_dao.update(db_obj=existing_file, obj_in={"content": content})
        if drop_missing_files:
            for path, existing_file in existing_files.items():
                if path not in seen:
                    await skill_file_dao.delete(id=existing_file.id)
        skill.files = list(await skill_file_dao.list_for_skill(skill.id))
        return skill


class SkillFileDAO(BaseDAO[SkillFileRecord]):
    """DAO for skill file rows."""

    table = "skill_files"
    columns = _SKILL_FILE_COLUMNS
    record_factory = staticmethod(SkillFileRecord.from_row)

    async def list_for_skill(self, skill_id: UUID) -> Sequence[SkillFileRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM skill_files WHERE skill_id = %(skill_id)s ORDER BY path",
                {"skill_id": skill_id},
            )
            return [SkillFileRecord.from_row(row) for row in rows]

    async def get_by_skill_and_path(self, skill_id: UUID, path: str) -> SkillFileRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM skill_files WHERE skill_id = %(skill_id)s AND path = %(path)s",
                {"skill_id": skill_id, "path": path},
            )
            return SkillFileRecord.from_row(row) if row else None

    async def delete_by_skill_and_path(self, skill_id: UUID, path: str) -> None:
        async with self.session() as db:
            await db.execute(
                "DELETE FROM skill_files WHERE skill_id = %(skill_id)s AND path = %(path)s",
                {"skill_id": skill_id, "path": path},
            )


skill_file_dao = SkillFileDAO()
skill_dao = SkillDAO()

"""Department path helpers for organization sync."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from app.dao.org_department_dao import org_department_dao


def build_department_path_map(departments: Sequence[Any]) -> dict[uuid.UUID, str]:
    """Build department name paths by walking the internal department tree."""
    dept_by_id = {dept.id: dept for dept in departments}
    paths: dict[uuid.UUID, str] = {}

    def is_virtual_root(dept: Any) -> bool:
        return not dept.parent_id and str(getattr(dept, "external_id", "") or "") == "0"

    def compute_path(dept_id: uuid.UUID, visited: set[uuid.UUID] | None = None) -> str:
        if dept_id in paths:
            return paths[dept_id]
        if visited is None:
            visited = set()
        if dept_id in visited:
            dept = dept_by_id.get(dept_id)
            fallback = (dept.name if dept else "") or ""
            paths[dept_id] = fallback
            return fallback

        visited.add(dept_id)
        dept = dept_by_id.get(dept_id)
        if not dept:
            return ""

        if is_virtual_root(dept):
            paths[dept_id] = ""
            return ""

        name = (dept.name or "").strip()
        if not dept.parent_id or dept.parent_id not in dept_by_id:
            paths[dept_id] = name
            return name

        parent_path = compute_path(dept.parent_id, visited)
        full_path = f"{parent_path}/{name}" if parent_path else name
        paths[dept_id] = full_path
        return full_path

    for dept in departments:
        compute_path(dept.id)

    return paths


async def derive_member_department_paths(
    db: Any,
    members: list[Any],
) -> dict[uuid.UUID, str]:
    """Resolve member department paths from department_id via the department tree."""
    del db
    dept_ids = {member.department_id for member in members if member.department_id}
    if not dept_ids:
        return {}

    departments: dict[uuid.UUID, Any] = {}
    pending_ids = set(dept_ids)

    while pending_ids:
        batch = []
        for dept_id in list(pending_ids):
            dept = await org_department_dao.get(dept_id)
            if dept:
                batch.append(dept)
        if not batch:
            break

        next_pending: set[uuid.UUID] = set()
        for department in batch:
            departments[department.id] = department
            if department.parent_id and department.parent_id not in departments:
                next_pending.add(department.parent_id)
        pending_ids = next_pending

    dept_path_map = build_department_path_map(list(departments.values()))

    return {member.id: dept_path_map.get(member.department_id, member.department_path or "") for member in members}

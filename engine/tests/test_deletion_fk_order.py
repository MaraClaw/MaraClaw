import re
from pathlib import Path

import pytest

from app.dao.agent_dao import AGENT_DELETE_CLEANUP_SQL
from app.dao.tenant_dao import TENANT_DELETE_CLEANUP_SQL


@pytest.mark.parametrize(
    "statements",
    [(*AGENT_DELETE_CLEANUP_SQL, "DELETE FROM agents WHERE id = %(aid)s"), TENANT_DELETE_CLEANUP_SQL],
    ids=["agent", "tenant"],
)
def test_cleanup_resolves_baseline_foreign_keys_before_deleting_parents(statements):
    baseline = (Path(__file__).resolve().parents[1] / "scripts/schema_baseline.sql").read_text()
    deletes = {}
    for index, statement in enumerate(statements):
        match = re.match(r"DELETE FROM (\w+)", statement)
        if match:
            deletes.setdefault(match[1], index)
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", baseline, re.S)
    tables.extend(re.findall(r"ALTER TABLE (\w+) ADD CONSTRAINT \w+ (FOREIGN KEY[^;]+);", baseline))
    checked = []
    for child, body in tables:
        for declaration in body.splitlines():
            reference = re.search(r"REFERENCES (\w+)\s*\(id\)", declaration)
            if reference is None:
                continue
            parent = reference[1]
            if child == "company_transfer_retained_agents":
                assert "ON DELETE RESTRICT" in declaration
                assert child not in deletes
                continue
            if (
                parent not in deletes
                or parent == child
                or "ON DELETE CASCADE" in declaration
                or "SET NULL" in declaration
            ):
                continue
            foreign_key = re.search(r"FOREIGN KEY\s*\((\w+)\)", declaration)
            column = foreign_key[1] if foreign_key else declaration.strip().split()[0]
            nullable_update = next(
                (
                    index
                    for index, statement in enumerate(statements)
                    if statement.split()[:3] == ["UPDATE", child, "SET"]
                    and re.search(r"\b" + re.escape(column) + r"\s*=\s*NULL\b", statement)
                ),
                None,
            )
            child_index = deletes.get(child, nullable_update)
            assert child_index is not None, f"{child} still references deleted {parent}"
            assert child_index < deletes[parent], f"{child} must be cleaned before {parent}"
            checked.append((child, parent))
    assert ("plaza_comments", "plaza_posts") in checked
    assert ("chat_messages", "participants") in checked
    assert ("task_logs", "tasks") in checked

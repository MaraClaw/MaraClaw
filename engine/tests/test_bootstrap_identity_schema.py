import re
from pathlib import Path

from app.scripts.bootstrap_db import _statement_bodies

BASELINE = Path(__file__).resolve().parents[1] / "scripts" / "schema_baseline.sql"


def test_baseline_omits_provider_department_objects() -> None:
    statements = _statement_bodies(BASELINE.read_text(encoding="utf-8"))

    identifiers = set(re.findall(r"\b[a-z_]+\b", "\n".join(statements)))

    assert "org_departments" not in identifiers
    assert "ix_org_departments_external_id" not in identifiers
    assert "ix_org_departments_tenant_id" not in identifiers


def test_baseline_retains_shared_member_identity_and_profile_columns() -> None:
    statements = _statement_bodies(BASELINE.read_text(encoding="utf-8"))

    members = next(
        statement for statement in statements if statement.startswith("CREATE TABLE IF NOT EXISTS org_members")
    )
    columns = set(re.findall(r"^\s+([a-z_]+)\s+", members, re.MULTILINE))

    assert columns == {
        "id",
        "open_id",
        "unionid",
        "external_id",
        "provider_id",
        "name",
        "name_translit_full",
        "name_translit_initial",
        "email",
        "avatar_url",
        "title",
        "phone",
        "status",
        "tenant_id",
        "user_id",
    }

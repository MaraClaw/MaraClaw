import re
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.domain.activity import ActivityAction
from app.core.domain.agents import AgentStatus, AgentType, PermissionGrantScope
from app.core.domain.approvals import ApprovalStatus
from app.core.domain.channels import ChannelType
from app.core.domain.tasks import TaskPriority, TaskStatus, TaskType
from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.schemas.schemas import TaskCreate, TaskUpdate


@pytest.mark.parametrize(
    ("enum_type", "sql_name"),
    [
        (UserRole, "user_role_enum"),
        (AgentStatus, "agent_status_enum"),
        (ActivityAction, "activity_action_enum"),
        (PermissionGrantScope, "permission_scope_enum"),
        (ApprovalStatus, "approval_status_enum"),
        (ChannelType, "channel_type_enum"),
        (TaskType, "task_type_enum"),
        (TaskStatus, "task_status_enum"),
        (TaskPriority, "task_priority_enum"),
    ],
)
def test_domain_values_match_existing_sql_enum(enum_type: type[StrEnum], sql_name: str) -> None:
    # Given the unchanged fresh-install schema.
    sql = (Path(__file__).parents[1] / "scripts/schema_baseline.sql").read_text()
    definition = re.search(rf"CREATE TYPE {sql_name} AS ENUM \(([^)]+)\)", sql)
    assert definition is not None
    # When comparing the independent SQL and Python contracts.
    sql_values = re.findall(r"'([^']+)'", definition.group(1))
    # Then no wire value is added, removed, or reordered.
    assert [member.value for member in enum_type] == sql_values


@pytest.mark.parametrize("enum_type", [UserRole, AgentStatus, AgentType, ChannelType, TaskStatus, ActivityAction])
def test_wire_roundtrip_preserves_plain_strings(enum_type: type[StrEnum]) -> None:
    # Given each existing wire value.
    adapter = TypeAdapter(enum_type)
    for member in enum_type:
        # When parsing and serializing the public value.
        parsed = adapter.validate_python(member.value)
        # Then domain code sees an enum and JSON remains the original string.
        assert parsed is member
        assert adapter.dump_json(parsed) == f'"{member.value}"'.encode()


@pytest.mark.parametrize("field", ["type", "priority"])
def test_task_create_rejects_unknown_closed_values(field: str) -> None:
    # Given an invalid finite-domain request value.
    request = {"title": "Follow up", field: "future-value"}
    # When parsing the request, then the invalid value cannot enter a service.
    with pytest.raises(ValidationError):
        TaskCreate.model_validate(request)


def test_task_update_rejects_unknown_status() -> None:
    # Given / When / Then an unknown task status fails at the request boundary.
    with pytest.raises(ValidationError):
        TaskUpdate.model_validate({"status": "future-value"})


def test_user_record_rejects_unknown_database_role() -> None:
    # Given a corrupt or incompatible stored role.
    row = {"id": uuid4(), "role": "custom_role"}
    # When parsing the database row, then no default privilege is substituted.
    with pytest.raises(ValueError):
        UserRecord.from_row(row)


def test_agent_record_rejects_unknown_database_status() -> None:
    # Given a stored agent with an unknown lifecycle status.
    row = {"id": uuid4(), "name": "Agent", "creator_id": uuid4(), "status": "future-value"}
    # When parsing the row, then the unknown state is not silently defaulted.
    with pytest.raises(ValueError):
        AgentRecord.from_row(row)


def test_agent_record_parses_database_agent_type_to_enum() -> None:
    # Given the stored VARCHAR text.
    row = {"id": uuid4(), "name": "Agent", "creator_id": uuid4(), "agent_type": "openclaw"}
    # When parsing the row, then domain code receives the enum.
    assert AgentRecord.from_row(row).agent_type is AgentType.OPENCLAW


@pytest.mark.parametrize("stored", [None, ""])
def test_agent_record_defaults_missing_database_agent_type(stored: str | None) -> None:
    # Given a row without a usable agent_type, as existing partial rows and defaults produce.
    row = {"id": uuid4(), "name": "Agent", "creator_id": uuid4(), "agent_type": stored}
    # When parsing the row, then the existing OpenClaw default is preserved.
    assert AgentRecord.from_row(row).agent_type is AgentType.OPENCLAW
    assert AgentRecord.from_row({k: v for k, v in row.items() if k != "agent_type"}).agent_type is AgentType.OPENCLAW


@pytest.mark.parametrize("stored", ["guest", "native", "OPENCLAW"])
def test_agent_record_rejects_unknown_database_agent_type(stored: str) -> None:
    # Given a corrupt or retired stored type.
    row = {"id": uuid4(), "name": "Agent", "creator_id": uuid4(), "agent_type": stored}
    # When parsing the row, then it is not coerced into OpenClaw.
    with pytest.raises(ValueError):
        AgentRecord.from_row(row)


def test_sealed_agent_preserves_explicit_no_company_access() -> None:
    # Given the existing retained-agent database sentinel, not an unknown grant level.
    row = {"id": uuid4(), "name": "Agent", "creator_id": uuid4(), "company_access_level": ""}
    # When parsing the retained agent.
    agent = AgentRecord.from_row(row)
    # Then the explicit empty wire value survives without gaining company access.
    assert agent.company_access_level == ""

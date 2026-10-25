import builtins
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.api import relationships
from app.core import permissions
from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord


@dataclass(frozen=True, slots=True)
class CandidateAgent:
    tenant_id: uuid.UUID
    access_mode: str = "company"


@dataclass(frozen=True, slots=True)
class CandidateMember:
    id: uuid.UUID
    name: str
    email: str | None = None
    title: str | None = None
    avatar_url: str | None = None
    external_id: str | None = None
    provider_id: uuid.UUID | None = None


type CandidateRow = tuple[CandidateMember, str | None, str | None, uuid.UUID | None]


@dataclass(frozen=True, slots=True)
class CandidateRowSpec:
    name: str
    provider_id: uuid.UUID | None = None
    provider_name: str | None = None
    provider_type: str | None = None
    linked_user_id: uuid.UUID | None = None


def _candidate_row(spec: CandidateRowSpec) -> CandidateRow:
    member = CandidateMember(id=uuid.uuid4(), name=spec.name, provider_id=spec.provider_id)
    return member, spec.provider_name, spec.provider_type, spec.linked_user_id


def _install_candidate_dependencies(monkeypatch: pytest.MonkeyPatch, rows: tuple[CandidateRow, ...]) -> Any:
    tenant_id = uuid.uuid4()
    agent = CandidateAgent(tenant_id=tenant_id)
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, role="org_admin")

    async def check_access(_user: Any, _agent_id: uuid.UUID) -> tuple[CandidateAgent, str]:
        return agent, "manage"

    async def platform_access(_user_id: uuid.UUID, _agent: CandidateAgent) -> str:
        return "manage"

    async def list_candidates(**_kwargs: Any) -> list[CandidateRow]:
        return list(rows)

    monkeypatch.setattr(relationships, "check_agent_access", check_access)
    monkeypatch.setattr(relationships, "get_agent_access_level_for_user_id", platform_access)
    monkeypatch.setattr(relationships.org_member_dao, "list_relationship_candidates", list_candidates)
    return current_user


@pytest.mark.asyncio
async def test_member_candidates_return_dao_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    current_user = _install_candidate_dependencies(monkeypatch, (_candidate_row(CandidateRowSpec(name="Alpha")),))

    # When
    candidates = await relationships.search_human_relationship_candidates(uuid.uuid4(), current_user=current_user)

    # Then
    assert [candidate["name"] for candidate in candidates] == ["Alpha"]
    assert candidates[0]["department_path"] == ""


@pytest.mark.asyncio
async def test_member_candidates_prefer_non_platform_provider_for_linked_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    linked_user_id = uuid.uuid4()
    platform_row = _candidate_row(
        CandidateRowSpec(
            name="Platform member",
            provider_id=uuid.uuid4(),
            provider_name="Web",
            provider_type="web",
            linked_user_id=linked_user_id,
        )
    )
    feishu_row = _candidate_row(
        CandidateRowSpec(
            name="Feishu member",
            provider_id=uuid.uuid4(),
            provider_name="Feishu",
            provider_type="feishu",
            linked_user_id=linked_user_id,
        )
    )
    current_user = _install_candidate_dependencies(monkeypatch, (platform_row, feishu_row))

    # When
    candidates = await relationships.search_human_relationship_candidates(uuid.uuid4(), current_user=current_user)

    # Then
    assert len(candidates) == 1
    assert candidates[0]["id"] == str(feishu_row[0].id)
    assert candidates[0]["provider_type"] == "feishu"


@pytest.mark.asyncio
async def test_member_candidates_apply_final_case_insensitive_rendered_name_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    current_user = _install_candidate_dependencies(
        monkeypatch,
        (
            _candidate_row(CandidateRowSpec(name="Zulu")),
            _candidate_row(CandidateRowSpec(name="alpha")),
        ),
    )
    rendered_sort_keys: list[str] = []

    def track_sort[T](values: Iterable[T], *, key: Callable[[T], str]) -> list[T]:
        ordered_values = list(values)
        if ordered_values and not isinstance(ordered_values[0], tuple):
            rendered_sort_keys.extend(key(candidate) for candidate in ordered_values)
        return builtins.sorted(ordered_values, key=key)

    monkeypatch.setattr(relationships, "sorted", track_sort, raising=False)

    # When
    candidates = await relationships.search_human_relationship_candidates(uuid.uuid4(), current_user=current_user)

    # Then
    assert [candidate["name"] for candidate in candidates] == ["alpha", "Zulu"]
    assert rendered_sort_keys == ["alpha", "zulu"]


@pytest.mark.asyncio
async def test_member_candidates_cap_case_insensitive_order_at_one_hundred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    names = tuple(f"{'candidate' if index % 2 == 0 else 'Candidate'}-{index:03d}" for index in range(101))
    rows = tuple(_candidate_row(CandidateRowSpec(name=name)) for name in reversed(names))
    current_user = _install_candidate_dependencies(monkeypatch, rows)

    # When
    candidates = await relationships.search_human_relationship_candidates(uuid.uuid4(), current_user=current_user)

    # Then
    assert [candidate["name"] for candidate in candidates] == list(names[:100])
    assert len(candidates) == 100
    assert "candidate-100" not in [candidate["name"] for candidate in candidates]


async def test_relationship_response_keeps_ids_and_empty_department_without_synced_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    tenant_id, user_id, agent_id, member_id, relationship_id = (uuid.uuid4() for _ in range(5))
    user = UserRecord(id=user_id, tenant_id=tenant_id, role=UserRole.ORG_ADMIN)
    agent = AgentRecord(id=agent_id, name="Agent", creator_id=user_id, tenant_id=tenant_id)
    member = OrgMemberRecord(id=member_id, name="Employee", tenant_id=tenant_id, user_id=user_id)
    relation = AgentRelationshipRecord(
        id=relationship_id,
        agent_id=agent_id,
        member_id=member_id,
        member=member,
        provider_name="Feishu",
        provider_type="feishu",
    )
    monkeypatch.setattr(relationships, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(relationships, "ensure_access_granted_platform_relationships", AsyncMock(return_value=False))
    monkeypatch.setattr(
        relationships.agent_relationship_dao,
        "list_for_agent_with_members_and_providers",
        AsyncMock(return_value=[relation]),
    )
    monkeypatch.setattr(relationships.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(permissions, "get_agent_access_level_for_user_id", AsyncMock(return_value="use"))
    # When
    result = await relationships.get_relationships(agent_id, current_user=user)
    # Then
    assert result[0]["id"] == str(relationship_id)
    assert result[0]["member_id"] == str(member_id)
    assert result[0]["access_allowed"] is True
    assert result[0]["member"] == {
        "name": "Employee",
        "title": "",
        "department_path": "",
        "avatar_url": None,
        "email": None,
        "provider_name": "Feishu",
        "provider_type": "feishu",
        "user_id": str(user_id),
        "is_platform_user": True,
    }

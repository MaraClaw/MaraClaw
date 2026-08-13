import builtins
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from app.api import relationships


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
    department_path: str | None = None
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

    async def check_access(_user: Any, _agent_id: uuid.UUID, _db: Any = None) -> tuple[CandidateAgent, str]:
        return agent, "manage"

    async def department_paths(_db: Any, _members: list[CandidateMember]) -> dict[uuid.UUID, str]:
        return {}

    async def platform_access(_db: Any, _user_id: uuid.UUID, _agent: CandidateAgent) -> str:
        return "manage"

    async def list_candidates(**_kwargs: Any) -> list[CandidateRow]:
        return list(rows)

    monkeypatch.setattr(relationships, "check_agent_access", check_access)
    monkeypatch.setattr(relationships, "derive_member_department_paths", department_paths)
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

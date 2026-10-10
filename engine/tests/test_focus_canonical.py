from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.focus import AgentFocusItemRecord
from app.records.okr import OKRKeyResultRecord, OKRObjectiveRecord, OKRProgressLogRecord
from app.services import focus_service, okr_scheduler


class FocusDAO:
    def __init__(self, rows: Sequence[AgentFocusItemRecord] = ()) -> None:
        self.rows: list[AgentFocusItemRecord] = list(rows)

    async def list_for_agent(self, agent_id: UUID, *, include_completed: bool = True) -> list[AgentFocusItemRecord]:
        return [r for r in self.rows if r.agent_id == agent_id and (include_completed or r.status != "completed")]

    async def get_by_key(self, agent_id: UUID, key: str) -> AgentFocusItemRecord | None:
        return next((r for r in self.rows if r.agent_id == agent_id and r.key == key), None)

    async def max_sort_order(self, agent_id: UUID) -> int:
        return max((r.sort_order for r in self.rows if r.agent_id == agent_id), default=0)

    async def create(self, *, obj_in: Mapping[str, object]) -> AgentFocusItemRecord:
        row = AgentFocusItemRecord.from_row({"id": uuid4(), **obj_in})
        self.rows.append(row)
        return row

    async def update(self, *, db_obj: AgentFocusItemRecord, obj_in: Mapping[str, object]) -> AgentFocusItemRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def count_for_agent(self, agent_id: UUID) -> int:
        return len(await self.list_for_agent(agent_id))

    async def bulk_insert_ignore(self, rows: list[dict[str, object]]) -> int:
        for row in rows:
            await self.create(obj_in=row)
        return len(rows)


@pytest.mark.parametrize("operation", ["list", "render", "upsert", "complete"])
async def test_legacy_file_does_not_create_focus_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    # Given: an empty canonical store and an importable historical file.
    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir()
    (workspace / "focus.md").write_text("# Focus\n- [ ] historical: must not import\n", encoding="utf-8")
    dao = FocusDAO()
    monkeypatch.setattr(focus_service, "agent_focus_item_dao", dao)
    monkeypatch.setattr(focus_service, "WORKSPACE_ROOT", tmp_path, raising=False)

    # When: a current focus operation runs.
    match operation:
        case "list":
            assert await focus_service.list_focus_items(agent_id) == []
        case "render":
            assert await focus_service.render_focus_context(agent_id) == ""
        case "upsert":
            await focus_service.upsert_focus_item(agent_id, key="current", description="DB focus")
        case "complete":
            assert await focus_service.complete_focus_item(agent_id, key="historical") is None

    # Then: only an explicitly requested canonical item can be created.
    assert [row.key for row in dao.rows] == (["current"] if operation == "upsert" else [])


async def test_focus_list_filters_completed_and_other_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: active, completed and foreign focus records.
    agent_id = uuid4()
    dao = FocusDAO(
        [
            AgentFocusItemRecord(uuid4(), agent_id, "active"),
            AgentFocusItemRecord(uuid4(), agent_id, "done", status="completed"),
            AgentFocusItemRecord(uuid4(), uuid4(), "foreign"),
        ]
    )
    monkeypatch.setattr(focus_service, "agent_focus_item_dao", dao)
    # When: only current items are requested.
    rows = await focus_service.list_focus_items(agent_id, include_completed=False)
    # Then: completion and agent scope are preserved.
    assert [row["key"] for row in rows] == ["active"]


async def test_focus_update_merges_metadata_and_reopens_item(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an existing completed canonical item.
    agent_id = uuid4()
    item = AgentFocusItemRecord(uuid4(), agent_id, "ship", status="completed", item_metadata={"kept": 1})
    dao = FocusDAO([item])
    monkeypatch.setattr(focus_service, "agent_focus_item_dao", dao)
    # When: the item is updated through the current service.
    result = await focus_service.upsert_focus_item(
        agent_id, key="ship", title="Launch", description="Deliver release", metadata={"added": 2}
    )
    # Then: it is updated, not duplicated, with metadata retained.
    assert len(dao.rows) == 1
    assert result["id"] == str(item.id)
    assert (result["title"], result["description"], result["status"], result["completed_at"]) == (
        "Launch",
        "Deliver release",
        "in_progress",
        None,
    )
    assert result["metadata"] == {"kept": 1, "added": 2}


async def test_complete_focus_scopes_the_update(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: two agents have the same focus key.
    agent_id = uuid4()
    own = AgentFocusItemRecord(uuid4(), agent_id, "ship")
    foreign = AgentFocusItemRecord(uuid4(), uuid4(), "ship")
    monkeypatch.setattr(focus_service, "agent_focus_item_dao", FocusDAO([own, foreign]))
    # When: the first agent completes its focus.
    result = await focus_service.complete_focus_item(agent_id, key="ship")
    # Then: the foreign record is untouched.
    assert result is not None
    assert result["status"] == "completed"
    assert result["completed_at"] is not None
    assert foreign.status == "in_progress"


async def test_render_focus_uses_db_items_and_caps_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: active, system and thirteen completed canonical records.
    agent_id = uuid4()
    rows = [
        AgentFocusItemRecord(uuid4(), agent_id, "ship", description="Release", title="Launch"),
        AgentFocusItemRecord(uuid4(), agent_id, "system:okr", description="Report", kind="system"),
        *[AgentFocusItemRecord(uuid4(), agent_id, f"done{i}", status="completed") for i in range(13)],
    ]
    monkeypatch.setattr(focus_service, "agent_focus_item_dao", FocusDAO(rows))
    # When: context is rendered.
    result = await focus_service.render_focus_context(agent_id)
    # Then: canonical keys appear with only the twelve most recent completed items.
    assert "(ship): Release" in result
    assert "system:okr: Report" in result
    assert "done11:" in result
    assert "done12:" not in result


@pytest.mark.parametrize(
    ("value", "status", "visible"),
    [
        (100.0, "completed", True),
        (75.0, "on_track", True),
        (50.0, "at_risk", True),
        (20.0, "behind", True),
        (10.0, "on_track", True),
        (75.0, "on_track", False),
    ],
)
async def test_okr_collects_canonical_progress_with_tenant_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: float, status: str, visible: bool
) -> None:
    # Given: DB progress differs from a stale legacy file; KR visibility is tenant-scoped.
    tenant_id, agent_id, okr_agent_id, kr_id = uuid4(), uuid4(), uuid4(), uuid4()
    objective = OKRObjectiveRecord(uuid4(), tenant_id, "Goal", "company", date(2026, 1, 1), date(2026, 12, 31))
    kr = OKRKeyResultRecord(kr_id, objective.id, "Ship", current_value=10.0)
    description = f"**KR ID**: {kr_id}\n**Current Progress**: {value}\n**This Week**: Released"
    dao = FocusDAO([AgentFocusItemRecord(uuid4(), agent_id, "ship", description=description)])
    monkeypatch.setattr(focus_service, "agent_focus_item_dao", dao)
    workspace = tmp_path / str(agent_id)
    workspace.mkdir()
    (workspace / "focus.md").write_text(f"## KR: stale\n**KR ID**: {kr_id}\n**Current Progress**: 999\n")
    monkeypatch.setattr(focus_service, "WORKSPACE_ROOT", tmp_path, raising=False)
    logs: list[OKRProgressLogRecord] = []

    async def agents(requested_tenant: UUID) -> list[AgentRecord]:
        assert requested_tenant == tenant_id
        return [AgentRecord(agent_id, "Member", uuid4()), AgentRecord(okr_agent_id, "OKR", uuid4())]

    async def get_kr(
        requested_kr: UUID, requested_tenant: UUID
    ) -> tuple[OKRKeyResultRecord, OKRObjectiveRecord] | None:
        assert (requested_kr, requested_tenant) == (kr_id, tenant_id)
        return (kr, objective) if visible else None

    async def update_kr(*, db_obj: OKRKeyResultRecord, obj_in: Mapping[str, object]) -> OKRKeyResultRecord:
        for key, field_value in obj_in.items():
            setattr(db_obj, key, field_value)
        return db_obj

    async def log_progress(*, obj_in: Mapping[str, object]) -> OKRProgressLogRecord:
        row = OKRProgressLogRecord.from_row({"id": uuid4(), **obj_in})
        logs.append(row)
        return row

    monkeypatch.setattr(okr_scheduler.agent_dao, "list_for_tenant", agents)
    monkeypatch.setattr(okr_scheduler.okr_key_result_dao, "get_with_tenant", get_kr)
    monkeypatch.setattr(okr_scheduler.okr_key_result_dao, "update", update_kr)
    monkeypatch.setattr(okr_scheduler.okr_progress_log_dao, "create", log_progress)
    # When: the OKR batch collects current focus progress.
    await okr_scheduler.collect_all_focus_updates(tenant_id, okr_agent_id)
    # Then: only visible, changed KR progress is persisted and logged.
    changed = visible and value != 10.0
    assert (kr.current_value, kr.status) == ((value, status) if changed else (10.0, "on_track"))
    assert len(logs) == int(changed)
    if changed:
        assert (logs[0].previous_value, logs[0].new_value, logs[0].source, logs[0].note) == (
            10.0,
            value,
            "okr_agent",
            "[focus] Released",
        )


@pytest.mark.parametrize(
    "description",
    [
        "ordinary focus",
        "**KR ID**: invalid\n**Current Progress**: 7",
        f"**KR ID**: {uuid4()}\n**Current Progress**: 1.2.3",
    ],
)
def test_okr_ignores_descriptions_without_valid_progress(description: str) -> None:
    # Given / When: a description has no valid KR progress payload.
    result = okr_scheduler._parse_focus_description(description)
    # Then: no update can be emitted.
    assert result == []

"""HTTP traversal and measured query work against opt-in, isolated PostgreSQL."""

import os
from collections.abc import Mapping
from dataclasses import asdict
from typing import Final
from uuid import UUID

import orjson
import pytest
from anyio import Path
from pydantic import TypeAdapter

from app.schemas.okr import ObjectiveOut, ObjectivePageOut
from okr_paging_postgres_fixtures import (
    COUNT,
    LEGACY_PATH,
    OWNER_TYPES,
    PAGE_PATH,
    PERIOD,
    OKRPostgres,
    SQLRead,
    kr_ids,
    objective_id,
    okr_postgres,
    owner_id,
    sql_reads,
)

__all__ = ["okr_postgres", "sql_reads"]
_OBJECTIVES: Final = TypeAdapter(list[ObjectiveOut])


async def _page(seeded: OKRPostgres, params: Mapping[str, str | int]) -> ObjectivePageOut:
    response = await seeded.client.get(PAGE_PATH, params={**PERIOD, **params})
    assert response.status_code == 200, response.text
    return ObjectivePageOut.model_validate_json(response.content)


@pytest.mark.parametrize("limit", [1, 7, 50])
async def test_live_tied_pages_return_every_complete_objective_once(okr_postgres: OKRPostgres, limit: int) -> None:
    legacy = await okr_postgres.client.get(LEGACY_PATH, params=PERIOD)
    assert legacy.status_code == 200, legacy.text
    full = _OBJECTIVES.validate_json(legacy.content)
    expected_indices = sorted(range(COUNT), key=lambda i: (OWNER_TYPES[i % 3], objective_id(i)))

    seen: list[ObjectiveOut] = []
    cursor: str | None = None
    for _ in range(COUNT // limit + 1):
        page = await _page(okr_postgres, {"limit": limit, **({"cursor": cursor} if cursor is not None else {})})
        assert len(page.items) <= limit
        seen.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert cursor is None, "Traversal did not terminate"
    assert [item.id for item in seen] == [str(objective_id(i)) for i in expected_indices]
    assert {item.id: item for item in seen} == {item.id: item for item in full}
    for i, item in zip(expected_indices, seen, strict=True):
        assert [kr.id for kr in item.key_results] == kr_ids(i)
        assert item.owner_name == (None if i % 3 == 1 else f"{'Agent' if i % 3 == 0 else 'User'} {i}")


@pytest.mark.parametrize(
    ("changes", "foreign"),
    [
        ({}, True),
        ({"period_start": "2026-01-02"}, False),
        ({"period_end": "2026-03-30"}, False),
        ({"owner_types": "agent"}, False),
    ],
    ids=["tenant", "period-start", "period-end", "owner-filter"],
)
async def test_live_cursor_rejects_scope_change(
    okr_postgres: OKRPostgres, sql_reads: list[SQLRead], changes: Mapping[str, str], foreign: bool
) -> None:
    first = await _page(okr_postgres, {"limit": 1})
    assert first.next_cursor is not None
    sql_reads.clear()

    response = await okr_postgres.client.get(
        PAGE_PATH,
        params={**PERIOD, "cursor": first.next_cursor, **changes},
        headers=okr_postgres.other_headers if foreign else {},
    )

    assert response.status_code == 400
    assert not [read for read in sql_reads if "FROM okr_objectives" in read.sql]


async def test_live_bad_cursor_empty_period_and_foreign_tenant(okr_postgres: OKRPostgres) -> None:
    first = await _page(okr_postgres, {"limit": 1})
    assert first.next_cursor is not None
    for token in ("!bad!", first.next_cursor + "corrupt"):
        rejected = await okr_postgres.client.get(PAGE_PATH, params={**PERIOD, "cursor": token})
        assert rejected.status_code == 400
    empty = await _page(okr_postgres, {"period_start": "2028-01-01", "period_end": "2028-03-31"})
    assert empty == ObjectivePageOut(items=[], next_cursor=None)
    foreign = await okr_postgres.client.get(PAGE_PATH, params=PERIOD, headers=okr_postgres.other_headers)
    assert foreign.status_code == 200
    other = ObjectivePageOut.model_validate_json(foreign.content)
    assert [item.id for item in other.items] == [str(UUID(int=4))]
    assert other.next_cursor is None


async def test_live_filtered_traversal_is_complete(okr_postgres: OKRPostgres) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        page = await _page(
            okr_postgres, {"owner_types": "user", "limit": 50, **({"cursor": cursor} if cursor is not None else {})}
        )
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert cursor is None
    assert seen == [str(objective_id(i)) for i in range(COUNT) if i % 3 == 2]


async def test_live_page_retains_hydration_bounds_and_records_same_dataset_measurements(
    okr_postgres: OKRPostgres, sql_reads: list[SQLRead]
) -> None:
    # Both requests see the exact same committed dataset; the observer executes every real query.
    response = await okr_postgres.client.get(PAGE_PATH, params={**PERIOD, "limit": 50})
    assert response.status_code == 200, response.text
    page = ObjectivePageOut.model_validate_json(response.content)
    page_reads = sql_reads.copy()
    sql_reads.clear()
    legacy_response = await okr_postgres.client.get(LEGACY_PATH, params=PERIOD)
    assert legacy_response.status_code == 200, legacy_response.text
    full = _OBJECTIVES.validate_json(legacy_response.content)
    full_reads = sql_reads.copy()

    retained = [UUID(item.id) for item in page.items]
    [objectives] = [read for read in page_reads if "FROM okr_objectives" in read.sql]
    [krs] = [read for read in page_reads if "FROM okr_key_results" in read.sql]
    [agents] = [read for read in page_reads if "FROM agents" in read.sql]
    assert objectives.rows == 51
    assert krs.params == {"ids": retained}
    assert krs.rows == 352  # The first retained objective has 205 KRs; the other 49 each have three.
    assert isinstance(agents.params, Mapping)
    assert set(TypeAdapter(list[UUID]).validate_python(agents.params["ids"])) == {owner_id(i) for i in range(0, 150, 3)}
    assert agents.rows == 50
    assert not [read for read in page_reads if "FROM users" in read.sql]
    assert len(page.items) == 50
    assert len(full) == COUNT
    assert sum(len(item.key_results) for item in full) == 2002
    assert len(response.content) < len(legacy_response.content)

    if (evidence_dir := os.environ.get("OKR_PAGING_EVIDENCE_DIR")) is not None:
        assert page.next_cursor is not None
        sql_reads.clear()
        _ = await _page(okr_postgres, {"limit": 50, "cursor": page.next_cursor})
        [continued] = [read for read in sql_reads if "FROM okr_objectives" in read.sql]
        [unlimited] = [read for read in full_reads if "FROM okr_objectives" in read.sql]
        plans = {}
        text: list[str] = []
        for name, read in (("page", objectives), ("continuation", continued), ("full", unlimited)):
            plans[name] = await okr_postgres.db.fetchval(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {read.sql}", read.params
            )
            rows = await okr_postgres.db.fetchall(f"EXPLAIN (ANALYZE, BUFFERS) {read.sql}", read.params)
            text.extend([f"== {name} ==", read.sql, *[str(row["QUERY PLAN"]) for row in rows], ""])
        path = Path(evidence_dir)
        await path.mkdir(parents=True, exist_ok=True)
        await (path / "plans.json").write_bytes(orjson.dumps(plans, option=orjson.OPT_INDENT_2))
        await (path / "plans.txt").write_text("\n".join(text))
        measurements = {
            "schema": okr_postgres.schema,
            "server_version": await okr_postgres.db.fetchval("SHOW server_version"),
            "dataset": {"qualifying_objectives": COUNT, "physical_objectives": 604, "key_results": 2002},
            "transport": "httpx ASGITransport with real bearer authentication and PostgreSQL",
            "page": {
                "objectives": len(page.items),
                "key_results": sum(len(item.key_results) for item in page.items),
                "wire_body_bytes": len(response.content),
                "reads": [asdict(read) for read in page_reads],
            },
            "full": {
                "objectives": len(full),
                "key_results": sum(len(item.key_results) for item in full),
                "wire_body_bytes": len(legacy_response.content),
                "reads": [asdict(read) for read in full_reads],
            },
        }
        await (path / "measurements.json").write_bytes(orjson.dumps(measurements, option=orjson.OPT_INDENT_2))

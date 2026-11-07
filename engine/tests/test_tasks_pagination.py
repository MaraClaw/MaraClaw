import base64
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import tasks as tasks_api
from app.core.domain.tasks import TaskStatus
from app.core.json_types import object_attr, object_mapping_from
from app.dao.task_dao import TaskDAO
from app.db import session as session_module
from app.records.task import TaskLogRecord, TaskRecord
from app.records.user import UserRecord
from app.schemas.schemas import TaskLogPage, TaskLogPageCursor, TaskPage, TaskPageCursor

AGENT_ID = UUID(int=1000)
OTHER_AGENT_ID = UUID(int=1001)
TASK_ID = UUID(int=2000)
SAME_MOMENT = datetime(2026, 11, 7, 1, 2, 3, tzinfo=UTC)
TASK_FROM = "tasks WHERE "
LOG_FROM = "task_logs WHERE task_id = %(task_id)s"
TASK_PAGE_ORDER = " ORDER BY created_at DESC, id DESC LIMIT %(lookahead)s"
LOG_PAGE_ORDER = " ORDER BY created_at ASC, id ASC LIMIT %(lookahead)s"


def task(number: int, *, agent_id: UUID = AGENT_ID, created_at: datetime = SAME_MOMENT) -> TaskRecord:
    return TaskRecord(
        id=UUID(int=number),
        agent_id=agent_id,
        title=f"task {number}",
        created_by=UUID(int=9),
        created_at=created_at,
        updated_at=created_at,
    )


def log(number: int, *, task_id: UUID = TASK_ID, created_at: datetime = SAME_MOMENT) -> TaskLogRecord:
    return TaskLogRecord(id=UUID(int=number), task_id=task_id, content=f"log {number}", created_at=created_at)


def _from(sql: str) -> str:
    return sql.split(" FROM ", 1)[1]


def _position(row: TaskRecord | TaskLogRecord) -> tuple[datetime, UUID]:
    assert row.created_at is not None
    return row.created_at, row.id


def _bound(values: dict[str, object], prefix: str) -> tuple[datetime, UUID] | None:
    if f"{prefix}_id" not in values:
        return None
    moment, row_id = values[f"{prefix}_created_at"], values[f"{prefix}_id"]
    assert isinstance(moment, datetime)
    assert isinstance(row_id, UUID)
    return moment, row_id


class TaskCursor:
    def __init__(self, raw: TaskRaw) -> None:
        self.raw: TaskRaw = raw
        self.results: list[dict[str, object]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: object = None) -> None:
        values = object_mapping_from(params)
        sql = " ".join(query.split())
        self.raw.queries.append((sql, values))
        limit = values.get("lookahead")
        if "FROM task_logs" in sql:
            after = _bound(values, "after")
            logs = sorted(
                (
                    r
                    for r in self.raw.logs
                    if r.task_id == values["task_id"] and (after is None or _position(r) > after)
                ),
                key=_position,
            )
            columns: tuple[str, ...] = ("id", "task_id", "content", "created_at")
            rows: list[TaskRecord | TaskLogRecord] = list(logs)
        else:
            before = _bound(values, "before")
            wanted = {k: values[k] for k in ("id", "status", "type") if k in values}
            tasks = sorted(
                (
                    r
                    for r in self.raw.tasks
                    if r.agent_id == values["agent_id"]
                    and all(str(object_attr(r, k)) == str(v) for k, v in wanted.items())
                    and (before is None or _position(r) < before)
                ),
                key=_position,
                reverse=("DESC" in sql),
            )
            columns = TaskDAO.columns
            rows = list(tasks)
        self.results = [
            {c: object_attr(r, c) for c in columns} for r in rows[: limit if isinstance(limit, int) else None]
        ]

    async def fetchone(self) -> dict[str, object] | None:
        return self.results[0] if self.results else None

    async def fetchall(self) -> list[dict[str, object]]:
        return self.results


class TaskRaw:
    def __init__(self) -> None:
        self.tasks: list[TaskRecord] = []
        self.logs: list[TaskLogRecord] = []
        self.queries: list[tuple[str, dict[str, object]]] = []

    def cursor(self) -> TaskCursor:
        return TaskCursor(self)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _Checkout:
    def __init__(self, raw: TaskRaw) -> None:
        self.raw: TaskRaw = raw

    async def __aenter__(self) -> TaskRaw:
        return self.raw

    async def __aexit__(self, *_args: object) -> bool:
        return False


class TaskPool:
    def __init__(self, raw: TaskRaw) -> None:
        self.raw: TaskRaw = raw

    def connection(self) -> _Checkout:
        return _Checkout(self.raw)


@dataclass(slots=True)
class BoardHTTP:
    client: httpx.AsyncClient
    raw: TaskRaw
    calls: list[str] = field(default_factory=list[str])
    denied_status: int | None = None

    def tasks_url(self, agent_id: UUID = AGENT_ID) -> str:
        return f"/agents/{agent_id}/tasks"

    def logs_url(self, task_id: UUID = TASK_ID, agent_id: UUID = AGENT_ID) -> str:
        return f"/agents/{agent_id}/tasks/{task_id}/logs/page"


@pytest.fixture
def task_raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[TaskRaw]:
    raw = TaskRaw()
    monkeypatch.setattr(session_module, "get_pool", lambda: TaskPool(raw))
    token = session_module._conn_ctx.set(None)
    try:
        yield raw
    finally:
        session_module._conn_ctx.reset(token)


@pytest.fixture
async def board(task_raw: TaskRaw, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[BoardHTTP]:
    user = UserRecord(id=UUID(int=4000), tenant_id=UUID(int=3000))
    app = FastAPI()
    app.include_router(tasks_api.router)
    app.dependency_overrides[tasks_api.get_current_user] = lambda: user
    result = BoardHTTP(
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://board.test"), task_raw
    )

    async def check_access(_user: UserRecord, agent_id: UUID) -> tuple[None, str]:
        result.calls.append("access")
        if result.denied_status is not None:
            raise HTTPException(result.denied_status, "denied")
        assert agent_id in (AGENT_ID, OTHER_AGENT_ID)
        return None, "manage"

    async def labels_for_ids(_ids: list[UUID]) -> dict[UUID, str | None]:
        return {UUID(int=9): "Creator"}

    monkeypatch.setattr(tasks_api, "check_agent_access", check_access)
    monkeypatch.setattr(tasks_api, "user_dao", SimpleNamespace(labels_for_ids=labels_for_ids))
    try:
        async with result.client:
            yield result
    finally:
        app.dependency_overrides.clear()


def _forged(token_model: TaskPageCursor | TaskLogPageCursor, **changes: object) -> str:
    payload = token_model.model_dump(mode="json") | changes
    raw = base64.urlsafe_b64encode(str(payload).replace("'", '"').replace("None", "null").encode())
    return raw.decode().rstrip("=")


async def test_page_ties_on_created_at_break_on_id_and_resume_exactly(board: BoardHTTP) -> None:
    # Given
    board.raw.tasks = [task(n) for n in range(1, 6)]
    # When
    first = TaskPage.model_validate_json(
        (await board.client.get(board.tasks_url() + "/page", params={"limit": 2})).content
    )
    assert first.next_cursor is not None
    board.raw.tasks.append(task(6))
    second = TaskPage.model_validate_json(
        (await board.client.get(board.tasks_url() + "/page", params={"limit": 2, "cursor": first.next_cursor})).content
    )
    assert second.next_cursor is not None
    third = TaskPage.model_validate_json(
        (await board.client.get(board.tasks_url() + "/page", params={"limit": 2, "cursor": second.next_cursor})).content
    )
    # Then
    assert [t.id.int for t in first.items] == [5, 4]
    assert [t.id.int for t in second.items] == [3, 2]
    assert [t.id.int for t in third.items] == [1]
    assert third.next_cursor is None
    assert first.items[0].creator_username == "Creator"
    sql, params = board.raw.queries[1]
    assert (
        _from(sql)
        == TASK_FROM
        + "agent_id = %(agent_id)s AND (created_at, id) < (%(before_created_at)s, %(before_id)s)"
        + TASK_PAGE_ORDER
    )
    assert params["lookahead"] == 3
    assert (params["before_created_at"], params["before_id"]) == (SAME_MOMENT, UUID(int=4))


async def test_page_filters_reach_sql_and_exact_boundary_has_no_cursor(board: BoardHTTP) -> None:
    # Given
    board.raw.tasks = [task(1), task(2), replace_status(task(3), TaskStatus.DOING)]
    # When
    response = await board.client.get(
        board.tasks_url() + "/page", params={"status_filter": "pending", "type_filter": "todo", "limit": 2}
    )
    # Then
    page = TaskPage.model_validate_json(response.content)
    assert [t.id.int for t in page.items] == [2, 1]
    assert page.next_cursor is None
    sql, params = board.raw.queries[0]
    assert (
        _from(sql)
        == TASK_FROM + "agent_id = %(agent_id)s AND status = %(status)s AND type = %(type)s" + TASK_PAGE_ORDER
    )
    assert (params["status"], params["type"]) == ("pending", "todo")


def replace_status(record: TaskRecord, status: TaskStatus) -> TaskRecord:
    record.status = status
    return record


async def test_task_cursor_bound_to_agent_and_filters_and_log_cursor_rejected(board: BoardHTTP) -> None:
    # Given
    board.raw.tasks = [task(n) for n in range(1, 4)]
    board.raw.logs = [log(n) for n in range(1, 4)]
    url = board.tasks_url() + "/page"
    first = TaskPage.model_validate_json(
        (await board.client.get(url, params={"limit": 1, "status_filter": "pending"})).content
    )
    assert first.next_cursor is not None
    log_cursor = TaskLogPageCursor(
        version=1, agent_id=AGENT_ID, task_id=TASK_ID, created_at=SAME_MOMENT, id=UUID(int=1)
    )
    board.raw.queries.clear()
    # When
    responses = [
        await board.client.get(url, params={"cursor": first.next_cursor}),
        await board.client.get(url, params={"cursor": first.next_cursor, "status_filter": "doing"}),
        await board.client.get(
            url, params={"cursor": first.next_cursor, "status_filter": "pending", "type_filter": "todo"}
        ),
        await board.client.get(
            board.tasks_url(OTHER_AGENT_ID) + "/page", params={"cursor": first.next_cursor, "status_filter": "pending"}
        ),
        await board.client.get(url, params={"cursor": log_cursor.encode()}),
    ]
    # Then
    assert [r.status_code for r in responses] == [400] * 5
    assert {r.json()["detail"] for r in responses} == {"Invalid cursor"}
    assert not board.raw.queries


@pytest.mark.parametrize(
    "mutate", ["suffix", "padding", "not-base64", "oversized", "version", "extra-field", "naive-time"]
)
async def test_tampered_cursors_are_400_on_both_routes(board: BoardHTTP, mutate: str) -> None:
    # Given
    board.raw.tasks = [task(1)]
    board.raw.logs = [log(1)]
    task_cursor = TaskPageCursor(
        version=1, agent_id=AGENT_ID, status=None, type=None, created_at=SAME_MOMENT, id=UUID(int=1)
    )
    log_cursor = TaskLogPageCursor(
        version=1, agent_id=AGENT_ID, task_id=TASK_ID, created_at=SAME_MOMENT, id=UUID(int=1)
    )
    board.raw.tasks.append(task(TASK_ID.int))
    tokens: list[str] = []
    for model in (task_cursor, log_cursor):
        valid = model.encode()
        match mutate:
            case "suffix":
                tokens.append(valid + "AAAA")
            case "padding":
                tokens.append(valid + "=")
            case "not-base64":
                tokens.append("!!not base64!!")
            case "oversized":
                tokens.append("A" * 2049)
            case "version":
                tokens.append(_forged(model, version=2))
            case "extra-field":
                tokens.append(_forged(model, extra=1))
            case "naive-time":
                tokens.append(_forged(model, created_at="2026-11-07T01:02:03"))
            case other:
                raise AssertionError(other)
    board.raw.queries.clear()
    # When
    task_response = await board.client.get(board.tasks_url() + "/page", params={"cursor": tokens[0]})
    log_response = await board.client.get(board.logs_url(), params={"cursor": tokens[1]})
    # Then
    assert (task_response.status_code, log_response.status_code) == (400, 400)
    assert not [q for q in board.raw.queries if "task_logs" in q[0] or "ORDER BY" in q[0]]


async def test_logs_page_ties_ascend_by_id_and_resume_exactly(board: BoardHTTP) -> None:
    # Given
    board.raw.tasks = [task(TASK_ID.int)]
    board.raw.logs = [log(n) for n in (3, 1, 2, 4, 5)]
    url = board.logs_url()
    # When
    first = TaskLogPage.model_validate_json((await board.client.get(url, params={"limit": 2})).content)
    assert first.next_cursor is not None
    board.raw.logs.append(log(6, created_at=SAME_MOMENT + timedelta(seconds=1)))
    second = TaskLogPage.model_validate_json(
        (await board.client.get(url, params={"limit": 2, "cursor": first.next_cursor})).content
    )
    assert second.next_cursor is not None
    third = TaskLogPage.model_validate_json(
        (await board.client.get(url, params={"limit": 2, "cursor": second.next_cursor})).content
    )
    # Then
    assert [r.id.int for r in first.items] == [1, 2]
    assert [r.id.int for r in second.items] == [3, 4]
    assert [r.id.int for r in third.items] == [5, 6]
    assert third.next_cursor is None
    sql, params = board.raw.queries[-1]
    assert _from(sql) == LOG_FROM + " AND (created_at, id) > (%(after_created_at)s, %(after_id)s)" + LOG_PAGE_ORDER
    assert params["lookahead"] == 3


async def test_logs_page_404_for_task_owned_by_another_agent_before_cursor_and_log_query(board: BoardHTTP) -> None:
    # Given
    board.raw.tasks = [task(TASK_ID.int, agent_id=OTHER_AGENT_ID)]
    board.raw.logs = [log(1)]
    # When
    plain = await board.client.get(board.logs_url())
    garbage = await board.client.get(board.logs_url(), params={"cursor": "garbage"})
    missing = await board.client.get(board.logs_url(task_id=UUID(int=404)))
    # Then
    assert [r.status_code for r in (plain, garbage, missing)] == [404, 404, 404]
    assert plain.json() == {"detail": "Task not found"}
    assert not [q for q in board.raw.queries if "task_logs" in q[0]]


async def test_log_cursor_bound_to_task_and_agent(board: BoardHTTP) -> None:
    # Given
    other_task = UUID(int=2001)
    board.raw.tasks = [task(TASK_ID.int), task(other_task.int), task(2002, agent_id=OTHER_AGENT_ID)]
    board.raw.logs = [log(1), log(2)]
    first = TaskLogPage.model_validate_json((await board.client.get(board.logs_url(), params={"limit": 1})).content)
    assert first.next_cursor is not None
    board.raw.queries.clear()
    # When
    other = await board.client.get(board.logs_url(other_task), params={"cursor": first.next_cursor})
    # Then
    assert other.status_code == 400
    assert not [q for q in board.raw.queries if "task_logs" in q[0]]
    cursor = TaskLogPageCursor.decode(first.next_cursor, AGENT_ID, TASK_ID)
    assert (cursor.id, cursor.created_at) == (first.items[-1].id, first.items[-1].created_at)
    with pytest.raises(ValueError, match="Invalid cursor"):
        _ = TaskLogPageCursor.decode(first.next_cursor, OTHER_AGENT_ID, TASK_ID)


@pytest.mark.parametrize("denial", [403, 404])
async def test_agent_access_error_precedes_every_lookup(board: BoardHTTP, denial: int) -> None:
    # Given
    board.denied_status = denial
    # When
    responses = [
        await board.client.get(board.tasks_url() + "/page", params={"cursor": "garbage"}),
        await board.client.get(board.logs_url(), params={"cursor": "garbage"}),
    ]
    # Then
    assert [r.status_code for r in responses] == [denial, denial]
    assert board.calls == ["access", "access"]
    assert not board.raw.queries


@pytest.mark.parametrize("limit", ["0", "101", "-1", "x"])
async def test_limit_outside_1_to_100_is_422_without_query(board: BoardHTTP, limit: str) -> None:
    # Given / When
    responses = [
        await board.client.get(board.tasks_url() + "/page", params={"limit": limit}),
        await board.client.get(board.logs_url(), params={"limit": limit}),
    ]
    # Then
    assert [r.status_code for r in responses] == [422, 422]
    assert not board.raw.queries


async def test_legacy_list_route_still_returns_complete_unbounded_history(board: BoardHTTP) -> None:
    # Given
    board.raw.tasks = [task(n, created_at=SAME_MOMENT + timedelta(seconds=n)) for n in range(1, 130)]
    # When
    response = await board.client.get(board.tasks_url() + "/")
    # Then
    assert response.status_code == 200
    assert len(response.json()) == 129
    sql, _params = board.raw.queries[0]
    assert _from(sql) == TASK_FROM + "agent_id = %(agent_id)s ORDER BY created_at DESC"


async def test_single_task_lookup_reconciles_terminal_status_and_hides_foreign_tasks(board: BoardHTTP) -> None:
    board.raw.tasks = [replace_status(task(TASK_ID.int), TaskStatus.DONE), task(2001, agent_id=OTHER_AGENT_ID)]
    response = await board.client.get(board.tasks_url() + f"/{TASK_ID}")
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["creator_username"] == "Creator"
    foreign = await board.client.get(board.tasks_url() + f"/{UUID(int=2001)}")
    assert foreign.status_code == 404
    board.denied_status = 403
    board.raw.queries.clear()
    denied = await board.client.get(board.tasks_url() + f"/{TASK_ID}")
    assert denied.status_code == 403
    assert not board.raw.queries

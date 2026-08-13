import asyncio

import pytest

from app.api.background_tasks import schedule_background_task


@pytest.mark.asyncio
async def test_schedule_background_task_runs_work_after_caller_returns() -> None:
    release = asyncio.Event()
    completed = asyncio.Event()

    async def work() -> None:
        await release.wait()
        completed.set()

    task = schedule_background_task(work(), "test task")

    assert not task.done()
    release.set()
    await task
    assert completed.is_set()

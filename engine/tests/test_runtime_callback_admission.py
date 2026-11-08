import asyncio
import threading
from concurrent.futures import CancelledError

import pytest

from app.runtime.callbacks import CallbackAdmission, CallbackRefusedError


async def test_sdk_submissions_are_bounded_before_launch_and_closed_submissions_never_start():
    owner = CallbackAdmission()
    entered = []

    async def work():
        entered.append(True)

    def burst():
        accepted, refused = [], []
        for _ in range(32):
            try:
                accepted.append(owner.submit(asyncio_loop, work))
            except CallbackRefusedError as error:
                refused.append(error)
        owner.close()
        return accepted, refused

    asyncio_loop = asyncio.get_running_loop()
    # Run without yielding the application loop so all admitted work is still pending.
    replies = []
    thread = threading.Thread(target=lambda: replies.append(burst()))
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    await owner.stop()
    accepted, refused = replies[0]
    assert len(accepted) == 8
    assert len(refused) == 24
    assert entered == []
    for future in accepted:
        with pytest.raises(CallbackRefusedError):
            future.result()


async def test_cancelled_sdk_submission_retains_capacity_until_application_cleanup_finishes():
    owner = CallbackAdmission()
    started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()

    futures = [owner.submit(asyncio.get_running_loop(), work) for _ in range(8)]
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert futures[0].cancel()
        await asyncio.wait_for(cancelling.wait(), 1)
        with pytest.raises(CallbackRefusedError, match="full"):
            owner.submit(asyncio.get_running_loop(), work)
        release.set()
        await owner.stop()
        assert not owner.tasks
        for future in futures:
            with pytest.raises(CancelledError):
                future.result()
    finally:
        release.set()
        await owner.stop()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_application_callback_releases_capacity_after_every_outcome(outcome):
    owner = CallbackAdmission()
    entered = asyncio.Event()

    async def first():
        entered.set()
        if outcome == "failure":
            raise ValueError("enrichment failed")
        if outcome == "cancel":
            await asyncio.Event().wait()

    first_task = asyncio.create_task(owner.run(first))
    await entered.wait()
    if outcome == "cancel":
        first_task.cancel()
    await asyncio.gather(first_task, return_exceptions=True)
    started, release = [], asyncio.Event()

    async def next_callback():
        started.append(True)
        await release.wait()

    admitted = [asyncio.create_task(owner.run(next_callback)) for _ in range(8)]
    try:
        await asyncio.sleep(0)
        assert len(started) == 8
    finally:
        release.set()
        await asyncio.gather(*admitted)
        await owner.stop()

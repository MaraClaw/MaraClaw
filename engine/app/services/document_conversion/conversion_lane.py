"""The one thread lane every blocking step of a document conversion runs on, and the staging of its output file."""

from __future__ import annotations

import threading
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import partial
from pathlib import Path

from app.config import get_settings
from app.core.logging import logger
from app.core.thread_lanes import ThreadLane

CONVERSION_LANE = ThreadLane(get_settings().DOCUMENT_CONVERSION_LANE_THREADS, name="conversion-lane")


class StagedOutput:
    """The private file one conversion writes instead of its target, until its caller publishes it.

    A lane thread cannot be interrupted, so a conversion that timed out or whose caller was cancelled may still write
    after the caller left. It writes here, beside the target (one filesystem, so the rename is atomic), and the caller
    publishes with a single `os.replace` only once the conversion finished in time; `discard` ends the stage otherwise.
    Both are one syscall on the event loop thread: they must not queue behind a lane that abandoned threads hold full.
    """

    def __init__(self, target: Path) -> None:
        self.target: Path = target
        self.path: Path = target.with_name(f".conversion-{uuid.uuid4().hex}{target.suffix}")
        self._discarded: threading.Event = threading.Event()

    def publish(self) -> None:
        """Replace the target with the staged file, atomically; a pre-existing target is never left half written."""
        _ = self.path.replace(self.target)

    def discard(self) -> None:
        """End the stage: remove the staged file now, and whatever a still running lane thread writes later."""
        self._discarded.set()
        self._remove()

    def guard[T](self, work: Callable[[], T]) -> Callable[[], T]:
        """`work` for a lane thread, which removes what it staged if it returns after the stage was discarded."""

        def guarded() -> T:
            try:
                return work()
            finally:
                if self._discarded.is_set():
                    self._remove()

        return guarded

    def _remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"Could not remove staged conversion output {self.path}: {exc}")


_STAGED: ContextVar[StagedOutput | None] = ContextVar("staged_conversion_output", default=None)


@asynccontextmanager
async def staged_output(target: Path) -> AsyncGenerator[StagedOutput]:
    """Stage the output of the conversion run inside the block; whatever is not published when it ends is discarded.

    Every `run_conversion` call made inside the block, `asyncio.timeout` or cancellation included, is guarded, so
    a thread that outlives the block deletes its own staged file when it returns instead of writing the target.
    """
    staged = StagedOutput(target)
    token = _STAGED.set(staged)
    try:
        yield staged
    finally:
        _STAGED.reset(token)
        staged.discard()


async def run_conversion[**P, T](work: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run `work` on the conversion lane, where `asyncio.to_thread` would borrow the default executor.

    The caller may leave at any time (its deadline, or a cancel) and `work` keeps running: the thread keeps its lane
    slot until it returns, so abandoned conversions can never push the lane past its size.
    """
    call = partial(work, *args, **kwargs)
    staged = _STAGED.get()
    return await CONVERSION_LANE.run(call if staged is None else staged.guard(call), abandon_on_cancel=True)

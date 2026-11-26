"""The one thread lane every blocking step of a document conversion runs on."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from app.config import get_settings
from app.core.thread_lanes import ThreadLane

CONVERSION_LANE = ThreadLane(get_settings().DOCUMENT_CONVERSION_LANE_THREADS, name="conversion-lane")


async def run_conversion[**P, T](work: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run `work` on the conversion lane, where `asyncio.to_thread` would borrow the default executor.

    The caller may leave at any time (its deadline, or a cancel) and `work` keeps running: the thread keeps its lane
    slot until it returns, so abandoned conversions can never push the lane past its size.
    """
    return await CONVERSION_LANE.run(partial(work, *args, **kwargs), abandon_on_cancel=True)

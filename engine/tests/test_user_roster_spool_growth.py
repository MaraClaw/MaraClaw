import gc
import os
import tracemalloc
from collections.abc import Callable

import user_roster_export_fixtures as fixtures
from app.services.user_roster_export import export_user_roster
from app.services.user_roster_response import _chunks
from user_roster_export_fixtures import TENANT, RosterDatabase, seed_matches

roster_db = fixtures.roster_db


async def test_growing_complete_export_keeps_application_csv_copies_bounded(
    roster_db: RosterDatabase, record_property: Callable[[str, object], None]
):
    measurements: list[tuple[int, int, int]] = []
    for added in (100, 10000):
        if added == 10000:
            await roster_db.writer.execute("DELETE FROM users WHERE display_name LIKE 'Match%'")
        await seed_matches(roster_db, added)
        await roster_db.writer.execute(
            "UPDATE users SET display_name=repeat('Match', 20) WHERE display_name LIKE 'Match%'"
        )
        gc.collect()
        tracemalloc.start()
        try:
            async with export_user_roster(TENANT, "Match", roster_db.actor) as spool:
                size = os.fstat(spool.fileno()).st_size
                delivered = 0
                async for chunk in _chunks(spool):
                    delivered += len(chunk)
                assert delivered == size
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        measurements.append((added, size, peak))
    small, large = measurements
    assert large[1] > small[1] * 90
    assert large[2] - small[2] < large[1] // 2
    record_property("roster_growth_rows_csv_bytes_python_peak", measurements)

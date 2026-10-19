from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.schedule_time import ScheduleTimezoneError
from app.services.scheduler import compute_next_run


@pytest.mark.parametrize(
    ("cron", "zone", "base", "expected"),
    [
        ("0 9 * * 1-5", "Asia/Ho_Chi_Minh", "2026-10-19T01:59:59+00:00", "2026-10-19T02:00:00+00:00"),
        ("0 9 * * 1-5", "America/New_York", "2026-03-06T14:00:00+00:00", "2026-03-09T13:00:00+00:00"),
        ("30 2 * * *", "America/New_York", "2026-03-07T07:30:00+00:00", "2026-03-09T06:30:00+00:00"),
        ("30 1 * * *", "America/New_York", "2026-11-01T05:29:59+00:00", "2026-11-01T05:30:00+00:00"),
        ("30 1 * * *", "America/New_York", "2026-11-01T05:30:00+00:00", "2026-11-02T06:30:00+00:00"),
        ("30 1 * * *", "America/New_York", "2026-11-01T06:15:00+00:00", "2026-11-02T06:30:00+00:00"),
        ("* * * * *", "America/New_York", "2026-11-01T06:15:30+00:00", "2026-11-01T07:00:00+00:00"),
        ("15 2 * * *", "Australia/Lord_Howe", "2026-10-02T15:45:00+00:00", "2026-10-04T15:15:00+00:00"),
        ("45 1 * * *", "Australia/Lord_Howe", "2026-04-04T14:44:59+00:00", "2026-04-04T14:45:00+00:00"),
        ("45 1 * * *", "Australia/Lord_Howe", "2026-04-04T15:05:00+00:00", "2026-04-05T15:15:00+00:00"),
        ("0 12 * * *", "UTC", "2026-10-15T12:00:00+00:00", "2026-10-16T12:00:00+00:00"),
    ],
)
def test_next_run_obeys_local_clock_gap_and_earlier_fold_policy(cron: str, zone: str, base: str, expected: str) -> None:
    base_instant = datetime.fromisoformat(base)
    result = compute_next_run(cron, base_instant, zone)
    assert result == datetime.fromisoformat(expected)
    assert result is not None
    assert result.tzinfo is UTC
    assert result > base_instant


def test_next_run_normalizes_aware_base_instead_of_relabeling() -> None:
    base = datetime(2026, 10, 19, 9, tzinfo=ZoneInfo("Asia/Tokyo"))
    result = compute_next_run("0 1 * * *", base)
    assert result == datetime(2026, 10, 19, 1, tzinfo=UTC)


def test_next_run_preserves_utc_default_and_invalid_cron_contract() -> None:
    base = datetime(2026, 10, 15, 12, tzinfo=UTC)
    assert compute_next_run("0 12 * * *", base) == datetime(2026, 10, 16, 12, tzinfo=UTC)
    assert compute_next_run("invalid", base) is None


@pytest.mark.parametrize("zone", ["Not/A_Zone", "", "/etc/localtime", "../UTC"])
def test_next_run_rejects_unknown_or_invalid_zone_without_host_fallback(zone: str) -> None:
    base = datetime(2026, 10, 15, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        compute_next_run("0 12 * * *", base, zone)


def test_next_run_reports_unavailable_timezone_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfoNotFoundError

    def unavailable(_key: str) -> ZoneInfo:
        raise ZoneInfoNotFoundError("Timezone database unavailable")

    monkeypatch.setattr("app.services.schedule_time.ZoneInfo", unavailable)
    with pytest.raises(ScheduleTimezoneError, match="timezone data unavailable") as caught:
        compute_next_run("0 12 * * *", datetime(2026, 10, 15, 12, tzinfo=UTC), "UTC")
    assert caught.value.timezone == "UTC"


def test_cron_targeting_only_spring_gaps_finishes_without_a_future_run() -> None:
    result = compute_next_run("30 2 * 3 0#2", datetime(2026, 1, 1, tzinfo=UTC), "America/New_York")
    assert result is None

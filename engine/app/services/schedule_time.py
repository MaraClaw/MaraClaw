"""Cron wall-clock interpretation with explicit gap and earlier-fold policies."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import override
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

MAX_SEARCH_YEARS = 50


@dataclass(slots=True)
class ScheduleTimezoneError(ValueError):
    timezone: str

    @override
    def __str__(self) -> str:
        return f"Invalid timezone {self.timezone!r}: unknown IANA zone or timezone data unavailable"


def load_timezone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleTimezoneError(timezone) from error


def validate_timezone(timezone: str) -> str:
    load_timezone(timezone)
    return timezone


def next_local_run(cron_expr: str, after: datetime, timezone: str) -> datetime | None:
    zone = load_timezone(timezone)
    base = after.replace(tzinfo=UTC) if after.tzinfo is None else after.astimezone(UTC)
    wall_base = base.astimezone(zone).replace(tzinfo=None)
    try:
        cron = croniter(cron_expr, wall_base, max_years_between_matches=MAX_SEARCH_YEARS)
        while True:
            wall = cron.get_next(datetime)
            if not isinstance(wall, datetime):
                raise TypeError("croniter.get_next(datetime) must return a datetime")
            if wall.year > wall_base.year + MAX_SEARCH_YEARS:
                return None
            candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
            if candidate > base and candidate.astimezone(zone).replace(tzinfo=None) == wall:
                return candidate
    except CroniterError:
        return None

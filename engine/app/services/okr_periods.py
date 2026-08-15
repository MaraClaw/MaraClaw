"""UTC OKR period math used by the dashboard router."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


def compute_current_period(frequency: str, length_days: int | None) -> tuple[date, date]:
    """Compute the start and end dates of the current OKR period."""
    today = datetime.now(UTC).date()
    return compute_period_for_date(frequency, length_days, today)


def compute_period_for_date(frequency: str, length_days: int | None, target: date) -> tuple[date, date]:
    """Compute the OKR period containing a specific date."""
    if frequency == "monthly":
        start = target.replace(day=1)
        if target.month == 12:
            end = target.replace(month=12, day=31)
        else:
            end = target.replace(month=target.month + 1, day=1) - timedelta(days=1)
    elif frequency == "custom" and length_days:
        epoch = date(1970, 1, 1)
        days_since_epoch = (target - epoch).days
        period_index = days_since_epoch // length_days
        start = epoch + timedelta(days=period_index * length_days)
        end = start + timedelta(days=length_days - 1)
    else:
        quarter = (target.month - 1) // 3 + 1
        start = date(target.year, (quarter - 1) * 3 + 1, 1)
        end = date(target.year, 12, 31) if quarter == 4 else date(target.year, quarter * 3 + 1, 1) - timedelta(days=1)
    return start, end


def advance_period(start: date, frequency: str, length_days: int | None, steps: int = 1) -> tuple[date, date]:
    """Move a period start forward by a fixed number of OKR periods."""
    if frequency == "monthly":
        month_index = start.year * 12 + (start.month - 1) + steps
        year = month_index // 12
        month = month_index % 12 + 1
        return compute_period_for_date(frequency, length_days, date(year, month, 1))
    if frequency == "custom" and length_days:
        next_start = start + timedelta(days=length_days * steps)
        return next_start, next_start + timedelta(days=length_days - 1)
    quarter = (start.month - 1) // 3
    quarter_index = start.year * 4 + quarter + steps
    year = quarter_index // 4
    next_quarter = quarter_index % 4 + 1
    return compute_period_for_date(frequency, length_days, date(year, (next_quarter - 1) * 3 + 1, 1))

"""Exact pricing of extra accounts and agents, and the Vietnam calendar arithmetic around it.

A quote counts today (Asia/Ho_Chi_Minh) as a charged day: with ``N`` days in the month and ``today.day`` the day of
month, ``N - today.day + 1`` days remain. All arithmetic is ``Fraction`` / integer; there is no float.
"""

import calendar
import math
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Final

GRACE_DAYS: Final = 7
VND_STEP: Final = 1000


def month_start(day: date) -> date:
    return day.replace(day=1)


def previous_month_start(month: date) -> date:
    return month_start(month_start(month) - timedelta(days=1))


def days_in_month(day: date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def remaining_days(today: date) -> int:
    """Days left in ``today``'s month, today included."""
    return days_in_month(today) - today.day + 1


@dataclass(frozen=True, slots=True)
class CapacityQuote:
    billing_month: date
    days_in_month: int
    days_charged: int
    extra_accounts: int
    extra_agents: int
    account_unit_usd: int
    agent_unit_usd: int
    monthly_usd: int
    amount_usd_cents: int
    usd_vnd_rate: int
    amount_vnd: int


def _quote(
    billing_month: date,
    days_charged: int,
    *,
    extra_accounts: int,
    extra_agents: int,
    account_unit_usd: int,
    agent_unit_usd: int,
    usd_vnd_rate: int,
) -> CapacityQuote:
    if extra_accounts < 0 or extra_agents < 0 or extra_accounts + extra_agents == 0:
        raise ValueError("A capacity order needs at least one extra account or agent")
    if usd_vnd_rate <= 0:
        raise ValueError("The USD/VND rate must be positive")
    total_days = days_in_month(billing_month)
    monthly_usd = extra_accounts * account_unit_usd + extra_agents * agent_unit_usd
    usd_exact = Fraction(monthly_usd * days_charged, total_days)
    amount_usd_cents = math.floor(usd_exact * 100 + Fraction(1, 2))
    amount_vnd = math.ceil(usd_exact * usd_vnd_rate / VND_STEP) * VND_STEP
    return CapacityQuote(
        billing_month=month_start(billing_month),
        days_in_month=total_days,
        days_charged=days_charged,
        extra_accounts=extra_accounts,
        extra_agents=extra_agents,
        account_unit_usd=account_unit_usd,
        agent_unit_usd=agent_unit_usd,
        monthly_usd=monthly_usd,
        amount_usd_cents=amount_usd_cents,
        usd_vnd_rate=usd_vnd_rate,
        amount_vnd=amount_vnd,
    )


def quote_purchase(
    today: date,
    *,
    extra_accounts: int,
    extra_agents: int,
    account_unit_usd: int,
    agent_unit_usd: int,
    usd_vnd_rate: int,
) -> CapacityQuote:
    """Prorated price of buying extras on ``today``: the remaining days of the month, today counted."""
    return _quote(
        today,
        remaining_days(today),
        extra_accounts=extra_accounts,
        extra_agents=extra_agents,
        account_unit_usd=account_unit_usd,
        agent_unit_usd=agent_unit_usd,
        usd_vnd_rate=usd_vnd_rate,
    )


def quote_renewal(
    billing_month: date,
    *,
    extra_accounts: int,
    extra_agents: int,
    account_unit_usd: int,
    agent_unit_usd: int,
    usd_vnd_rate: int,
) -> CapacityQuote:
    """Price of a whole month of extras (renewals are never prorated)."""
    return _quote(
        billing_month,
        days_in_month(billing_month),
        extra_accounts=extra_accounts,
        extra_agents=extra_agents,
        account_unit_usd=account_unit_usd,
        agent_unit_usd=agent_unit_usd,
        usd_vnd_rate=usd_vnd_rate,
    )

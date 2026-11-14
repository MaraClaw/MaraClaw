"""Exact proration, USD cents and VND rounding of capacity orders."""

from datetime import date

import pytest

from app.records.organization_plan import OrganizationAdditionalAccountMonthlyUsd, OrganizationAdditionalAgentMonthlyUsd
from app.services.capacity_pricing import (
    GRACE_DAYS,
    days_in_month,
    month_start,
    previous_month_start,
    quote_purchase,
    quote_renewal,
    remaining_days,
)

PRO_ACCOUNT = int(OrganizationAdditionalAccountMonthlyUsd.PRO)
PRO_AGENT = int(OrganizationAdditionalAgentMonthlyUsd.PRO)


def purchase(today: date, accounts: int = 0, agents: int = 0, rate: int = 25_000):
    return quote_purchase(
        today,
        extra_accounts=accounts,
        extra_agents=agents,
        account_unit_usd=PRO_ACCOUNT,
        agent_unit_usd=PRO_AGENT,
        usd_vnd_rate=rate,
    )


@pytest.mark.parametrize(
    ("day", "month_days", "remaining"),
    [
        (date(2027, 1, 1), 31, 31),
        (date(2027, 1, 31), 31, 1),
        (date(2026, 11, 14), 30, 17),
        (date(2026, 11, 30), 30, 1),
        (date(2027, 2, 1), 28, 28),
        (date(2027, 2, 28), 28, 1),
        (date(2028, 2, 1), 29, 29),
        (date(2028, 2, 29), 29, 1),
    ],
)
def test_the_remaining_days_count_today_in_the_vietnam_month(day: date, month_days: int, remaining: int):
    assert days_in_month(day) == month_days
    assert remaining_days(day) == remaining


def test_month_helpers_cross_year_boundaries():
    assert month_start(date(2026, 12, 31)) == date(2026, 12, 1)
    assert previous_month_start(date(2027, 1, 1)) == date(2026, 12, 1)
    assert previous_month_start(date(2027, 3, 17)) == date(2027, 2, 1)
    assert GRACE_DAYS == 7


def test_buying_on_the_first_costs_the_whole_month():
    quote = purchase(date(2027, 1, 1), accounts=1)
    assert (quote.days_in_month, quote.days_charged, quote.monthly_usd) == (31, 31, 5)
    assert quote.amount_usd_cents == 500
    assert quote.amount_vnd == 125_000
    assert quote.billing_month == date(2027, 1, 1)


def test_buying_on_the_last_day_of_a_31_day_month_costs_one_thirty_first():
    quote = purchase(date(2027, 1, 31), accounts=1)
    assert (quote.days_charged, quote.days_in_month) == (1, 31)
    # 5 / 31 = 0.16129 USD: 16 cents; 0.16129 * 25000 / 1000 = 4.03 thousand VND rounds up to 5.
    assert quote.amount_usd_cents == 16
    assert quote.amount_vnd == 5_000


def test_a_leap_february_has_twenty_nine_days():
    quote = purchase(date(2028, 2, 29), accounts=1, agents=1, rate=25_500)
    assert (quote.days_charged, quote.days_in_month, quote.monthly_usd) == (1, 29, 40)
    # 40 / 29 = 1.37931 USD: 138 cents; 1.37931 * 25500 / 1000 = 35.17 rounds up to 36.
    assert quote.amount_usd_cents == 138
    assert quote.amount_vnd == 36_000


def test_a_common_february_has_twenty_eight_days():
    first = purchase(date(2027, 2, 1), agents=1)
    assert (first.days_charged, first.days_in_month) == (28, 28)
    assert first.amount_usd_cents == 3_500
    last = purchase(date(2027, 2, 28), agents=1)
    # 35 / 28 = 1.25 USD exactly.
    assert last.amount_usd_cents == 125
    assert last.amount_vnd == 32_000  # 1.25 * 25000 = 31250 VND, up to the next thousand


def test_mid_month_proration_rounds_cents_half_up_and_vnd_up():
    quote = purchase(date(2026, 11, 14), accounts=2, agents=3, rate=26_000)
    assert (quote.days_charged, quote.days_in_month, quote.monthly_usd) == (17, 30, 115)
    # 115 * 17 / 30 = 65.1666 USD: 6516.67 cents rounds to 6517; 65.1666 * 26000 / 1000 = 1694.33 rounds up to 1695.
    assert quote.amount_usd_cents == 6_517
    assert quote.amount_vnd == 1_695_000


def test_cents_round_down_below_a_half():
    quote = purchase(date(2026, 11, 14), accounts=1)
    # 5 * 17 / 30 = 2.8333 USD: 283.33 cents rounds to 283.
    assert quote.amount_usd_cents == 283


def test_an_exact_thousand_is_not_rounded_up():
    quote = purchase(date(2027, 1, 1), accounts=1, rate=24_000)
    assert quote.amount_vnd == 120_000


def test_the_vnd_amount_comes_from_the_exact_usd_amount_not_from_rounded_cents():
    quote = quote_purchase(
        date(2026, 11, 14),
        extra_accounts=1,
        extra_agents=0,
        account_unit_usd=5,
        agent_unit_usd=35,
        usd_vnd_rate=1_000_000,
    )
    # Rounded cents (2.83 USD) would give 2_830_000; the exact 2.8333 USD is 2_833_333 VND, up to 2_834_000.
    assert quote.amount_vnd == 2_834_000


@pytest.mark.parametrize("today", [date(2027, 1, 1), date(2027, 2, 14), date(2028, 2, 29), date(2026, 11, 30)])
def test_the_amount_never_grows_as_the_month_goes_on_and_amounts_are_exact_integers(today: date):
    previous = None
    for day in range(1, days_in_month(today) + 1):
        quote = purchase(today.replace(day=day), accounts=3, agents=2, rate=25_321)
        assert isinstance(quote.amount_usd_cents, int)
        assert isinstance(quote.amount_vnd, int)
        assert quote.amount_vnd % 1000 == 0
        assert quote.days_charged == days_in_month(today) - day + 1
        if previous is not None:
            assert quote.amount_vnd <= previous
        previous = quote.amount_vnd


def test_a_renewal_is_always_the_whole_month():
    quote = quote_renewal(
        date(2028, 2, 1),
        extra_accounts=0,
        extra_agents=1,
        account_unit_usd=PRO_ACCOUNT,
        agent_unit_usd=PRO_AGENT,
        usd_vnd_rate=25_000,
    )
    assert (quote.days_charged, quote.days_in_month) == (29, 29)
    assert quote.amount_usd_cents == 3_500
    assert quote.amount_vnd == 875_000
    assert quote.billing_month == date(2028, 2, 1)


def test_a_renewal_equals_a_purchase_made_on_the_first():
    first = purchase(date(2026, 12, 1), accounts=4, agents=2, rate=25_100)
    renewal = quote_renewal(
        date(2026, 12, 1),
        extra_accounts=4,
        extra_agents=2,
        account_unit_usd=PRO_ACCOUNT,
        agent_unit_usd=PRO_AGENT,
        usd_vnd_rate=25_100,
    )
    assert first == renewal


@pytest.mark.parametrize(("accounts", "agents"), [(0, 0), (-1, 2), (2, -1)])
def test_a_quote_needs_nonnegative_quantities_with_a_positive_sum(accounts: int, agents: int):
    with pytest.raises(ValueError, match="at least one"):
        _ = purchase(date(2027, 1, 1), accounts=accounts, agents=agents)


@pytest.mark.parametrize("rate", [0, -25_000])
def test_a_quote_needs_a_positive_rate(rate: int):
    with pytest.raises(ValueError, match="rate"):
        _ = purchase(date(2027, 1, 1), accounts=1, rate=rate)

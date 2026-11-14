"""What an organization admission costs: the first period of a plan, converted to dong."""

import re
from fractions import Fraction

import pytest

from app.records.capacity_order import new_reference
from app.records.organization_plan import OrganizationBillingCycle
from app.services.capacity_pricing import VND_STEP, round_up_vnd
from app.services.organization_plans import ORGANIZATION_PLANS, first_period_price_usd


@pytest.mark.parametrize("plan", ORGANIZATION_PLANS, ids=lambda plan: plan.id)
def test_the_first_period_is_the_monthly_or_the_annual_price_without_proration(plan):
    assert first_period_price_usd(plan, OrganizationBillingCycle.MONTHLY) == plan.monthly_price_usd
    assert first_period_price_usd(plan, OrganizationBillingCycle.ANNUAL) == int(plan.annual_price_usd)


def test_the_catalog_prices_are_the_published_ones():
    prices = {
        plan.id: (
            first_period_price_usd(plan, OrganizationBillingCycle.MONTHLY),
            first_period_price_usd(plan, OrganizationBillingCycle.ANNUAL),
        )
        for plan in ORGANIZATION_PLANS
    }
    assert prices == {"pro": (79, 790), "max": (249, 2490), "ultra": (699, 6990)}


@pytest.mark.parametrize(
    ("usd", "rate", "expected"),
    [
        (79, 25_000, 1_975_000),
        (79, 24_567, 1_941_000),
        (790, 24_567, 19_408_000),
        (249, 25_001, 6_226_000),
        (1, 1, VND_STEP),
        (0, 25_000, 0),
        (Fraction(1, 3), 25_000, 9_000),
    ],
)
def test_vnd_amounts_round_up_to_the_next_thousand_and_exact_thousands_stay(usd, rate, expected):
    assert round_up_vnd(usd, rate) == expected
    assert round_up_vnd(usd, rate) % VND_STEP == 0


def test_an_admission_reference_is_the_prefix_and_eight_uppercase_base32_characters():
    references = {new_reference("MO") for _ in range(200)}
    assert len(references) == 200
    assert all(re.fullmatch(r"MO[A-Z2-7]{8}", reference) for reference in references)

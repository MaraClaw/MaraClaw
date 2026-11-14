from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.records.organization_admission import OrganizationAdmissionRecord
from app.records.organization_plan import (
    OrganizationAdditionalAccountMonthlyUsd,
    OrganizationAdditionalAgentMonthlyUsd,
    OrganizationAgentLimit,
    OrganizationAnnualPriceUsd,
    OrganizationCurrency,
    OrganizationDailyAgentMessageLimit,
    OrganizationFileLimitBytes,
    OrganizationGuestCpuShares,
    OrganizationGuestMemoryBytes,
    OrganizationGuestMillicpus,
    OrganizationGuestPidsLimit,
    OrganizationIncludedAgents,
    OrganizationPlan,
    OrganizationPlanId,
    OrganizationPlanName,
    OrganizationStorageLimitBytes,
    OrganizationWorkspaceDiskBytes,
)
from app.services.organization_plans import ORGANIZATION_PLANS, plan_for

_GUEST_RESOURCE_FIELDS = (
    "guest_memory_bytes",
    "guest_millicpus",
    "guest_cpu_shares",
    "guest_pids_limit",
    "workspace_disk_bytes",
)
_QUOTED_ENUM_FIELDS = ("annual_price_usd", "included_agents", "additional_agent_monthly_usd")
_ADDON_FIELDS = (
    "storage_addon_bytes",
    "storage_addon_monthly_usd",
    "workspace_disk_addon_bytes",
    "workspace_disk_addon_monthly_usd",
)


def test_catalog_grants_two_three_and_five_agents_per_account():
    assert [(plan.id, plan.max_agents_per_account) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 2),
        (OrganizationPlanId.MAX, 3),
        (OrganizationPlanId.ULTRA, 5),
    ]
    assert [plan.max_agents_per_account for plan in ORGANIZATION_PLANS] == list(OrganizationAgentLimit)


def test_catalog_quotes_included_agents_and_additional_agent_prices():
    assert [(plan.id, plan.included_agents, plan.additional_agent_monthly_usd) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 2, 35),
        (OrganizationPlanId.MAX, 5, 49),
        (OrganizationPlanId.ULTRA, 10, 79),
    ]
    assert [plan.included_agents for plan in ORGANIZATION_PLANS] == list(OrganizationIncludedAgents)
    assert [plan.additional_agent_monthly_usd for plan in ORGANIZATION_PLANS] == list(
        OrganizationAdditionalAgentMonthlyUsd
    )


def test_catalog_quotes_the_same_storage_and_workspace_disk_addons_for_every_tier():
    assert {
        (
            plan.storage_addon_bytes,
            plan.storage_addon_monthly_usd,
            plan.workspace_disk_addon_bytes,
            plan.workspace_disk_addon_monthly_usd,
        )
        for plan in ORGANIZATION_PLANS
    } == {(10_737_418_240, 5, 5_368_709_120, 3)}


def test_catalog_annual_price_is_ten_times_the_monthly_price():
    assert [(plan.id, plan.annual_price_usd) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 790),
        (OrganizationPlanId.MAX, 2490),
        (OrganizationPlanId.ULTRA, 6990),
    ]
    assert [plan.annual_price_usd for plan in ORGANIZATION_PLANS] == list(OrganizationAnnualPriceUsd)
    for plan in ORGANIZATION_PLANS:
        assert plan.annual_price_usd == 10 * plan.monthly_price_usd


def test_catalog_names_each_tier_from_the_plan_name_enum():
    assert [(plan.id, plan.name) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, "Pro"),
        (OrganizationPlanId.MAX, "Max"),
        (OrganizationPlanId.ULTRA, "Ultra"),
    ]
    assert [plan.name for plan in ORGANIZATION_PLANS] == list(OrganizationPlanName)


def test_catalog_grants_per_agent_storage_and_file_byte_limits():
    assert [(plan.id, plan.storage_limit_bytes, plan.max_file_size_bytes) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 2_147_483_648, 26_214_400),
        (OrganizationPlanId.MAX, 5_368_709_120, 52_428_800),
        (OrganizationPlanId.ULTRA, 10_737_418_240, 104_857_600),
    ]
    assert [plan.storage_limit_bytes for plan in ORGANIZATION_PLANS] == list(OrganizationStorageLimitBytes)
    assert [plan.max_file_size_bytes for plan in ORGANIZATION_PLANS] == list(OrganizationFileLimitBytes)


def test_catalog_grants_per_agent_daily_messages():
    assert [(plan.id, plan.daily_messages_per_agent) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 100),
        (OrganizationPlanId.MAX, 200),
        (OrganizationPlanId.ULTRA, 500),
    ]
    assert [plan.daily_messages_per_agent for plan in ORGANIZATION_PLANS] == list(OrganizationDailyAgentMessageLimit)


def test_catalog_grants_per_guest_memory():
    assert [(plan.id, plan.guest_memory_bytes) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 2_147_483_648),
        (OrganizationPlanId.MAX, 3_221_225_472),
        (OrganizationPlanId.ULTRA, 5_368_709_120),
    ]
    assert [plan.guest_memory_bytes for plan in ORGANIZATION_PLANS] == list(OrganizationGuestMemoryBytes)


def test_catalog_grants_per_guest_cpu_ceiling():
    assert [(plan.id, plan.guest_millicpus) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 1500),
        (OrganizationPlanId.MAX, 2000),
        (OrganizationPlanId.ULTRA, 3000),
    ]
    assert [plan.guest_millicpus for plan in ORGANIZATION_PLANS] == list(OrganizationGuestMillicpus)


def test_catalog_grants_per_guest_cpu_weight():
    assert [(plan.id, plan.guest_cpu_shares) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 1024),
        (OrganizationPlanId.MAX, 1536),
        (OrganizationPlanId.ULTRA, 2048),
    ]
    assert [plan.guest_cpu_shares for plan in ORGANIZATION_PLANS] == list(OrganizationGuestCpuShares)


def test_catalog_grants_per_guest_process_limit():
    assert [(plan.id, plan.guest_pids_limit) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 512),
        (OrganizationPlanId.MAX, 768),
        (OrganizationPlanId.ULTRA, 1024),
    ]
    assert [plan.guest_pids_limit for plan in ORGANIZATION_PLANS] == list(OrganizationGuestPidsLimit)


def test_catalog_grants_per_agent_workspace_disk():
    assert [(plan.id, plan.workspace_disk_bytes) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 3_221_225_472),
        (OrganizationPlanId.MAX, 5_368_709_120),
        (OrganizationPlanId.ULTRA, 8_589_934_592),
    ]
    assert [plan.workspace_disk_bytes for plan in ORGANIZATION_PLANS] == list(OrganizationWorkspaceDiskBytes)


def test_catalog_prices_the_organization_fee_per_month():
    assert [(plan.id, plan.monthly_price_usd, plan.included_accounts) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 79, 5),
        (OrganizationPlanId.MAX, 249, 15),
        (OrganizationPlanId.ULTRA, 699, 40),
    ]


def test_catalog_quotes_additional_account_prices_without_an_account_ceiling():
    assert [(plan.id, plan.additional_account_monthly_usd) for plan in ORGANIZATION_PLANS] == [
        (OrganizationPlanId.PRO, 5),
        (OrganizationPlanId.MAX, 4),
        (OrganizationPlanId.ULTRA, 3),
    ]
    assert [plan.additional_account_monthly_usd for plan in ORGANIZATION_PLANS] == list(
        OrganizationAdditionalAccountMonthlyUsd
    )
    assert "max_accounts" not in OrganizationPlan.model_fields


def test_current_snapshot_round_trips_as_current():
    raw = plan_for(OrganizationPlanId.ULTRA).model_dump(mode="json")
    stored = OrganizationPlan.model_validate(raw)
    assert stored.name is OrganizationPlanName.ULTRA
    assert raw["name"] == "Ultra"
    assert stored.currency is OrganizationCurrency.USD
    assert raw["currency"] == "USD"
    assert stored.max_agents_per_account is OrganizationAgentLimit.ULTRA
    assert stored.storage_limit_bytes is OrganizationStorageLimitBytes.ULTRA
    assert stored.max_file_size_bytes is OrganizationFileLimitBytes.ULTRA
    assert stored.daily_messages_per_agent is OrganizationDailyAgentMessageLimit.ULTRA
    assert stored.guest_memory_bytes is OrganizationGuestMemoryBytes.ULTRA
    assert stored.guest_millicpus is OrganizationGuestMillicpus.ULTRA
    assert stored.guest_cpu_shares is OrganizationGuestCpuShares.ULTRA
    assert stored.guest_pids_limit is OrganizationGuestPidsLimit.ULTRA
    assert stored.workspace_disk_bytes is OrganizationWorkspaceDiskBytes.ULTRA
    assert stored.additional_account_monthly_usd is OrganizationAdditionalAccountMonthlyUsd.ULTRA
    assert (raw["monthly_price_usd"], raw["additional_account_monthly_usd"]) == (699, 3)
    assert stored.annual_price_usd is OrganizationAnnualPriceUsd.ULTRA
    assert stored.included_agents is OrganizationIncludedAgents.ULTRA
    assert stored.additional_agent_monthly_usd is OrganizationAdditionalAgentMonthlyUsd.ULTRA
    assert (raw["annual_price_usd"], raw["included_agents"], raw["additional_agent_monthly_usd"]) == (6990, 10, 79)
    assert (raw["storage_addon_bytes"], raw["storage_addon_monthly_usd"]) == (10_737_418_240, 5)
    assert (raw["workspace_disk_addon_bytes"], raw["workspace_disk_addon_monthly_usd"]) == (5_368_709_120, 3)
    assert raw["storage_limit_bytes"] == 10_737_418_240
    assert raw["max_file_size_bytes"] == 104_857_600
    assert raw["daily_messages_per_agent"] == 500
    assert raw["guest_memory_bytes"] == 5_368_709_120
    assert raw["guest_millicpus"] == 3000
    assert raw["guest_cpu_shares"] == 2048
    assert raw["guest_pids_limit"] == 1024
    assert raw["workspace_disk_bytes"] == 8_589_934_592
    assert stored.model_dump(mode="json") == raw


def _current() -> dict[str, object]:
    return TypeAdapter(dict[str, object]).validate_python(plan_for(OrganizationPlanId.MAX).model_dump(mode="json"))


def _old_wire_format() -> dict[str, object]:
    """The pre-enforcement organization-wide allocation; a fresh deployment never stores or accepts it."""
    return {
        **{key: value for key, value in _current().items() if key != "max_agents_per_account"},
        "max_digital_employees": 6,
    }


_BAD_BYTES = (None, True, 0, 1024, 26_214_401, 1_073_741_825, 1_073_741_824.5)
_BAD_PRICES = (None, True, 0, 1, 2, 6, 7, 12, 100, 12.5)
_BAD_CAPS = (None, True, 0, 1, 4, 6, 10, 11, 2.5)
_BAD_ANNUAL = (None, True, 0, 1, 79, 1000, 7000, 2490.5)
_BAD_INCLUDED = (None, True, 0, 1, 3, 4, 11, 5.5)
_BAD_AGENT_PRICES = (None, True, 0, 1, 34, 50, 100, 49.5)
_BAD_ADDONS = (None, 0, -1, 1.5, "ten")
_BAD_DAILY = (None, True, 0, 1, 99, 101, 250, 501, 100.5)
_BAD_NAMES = (None, True, 0, "", "pro", "PRO", "Pro ", "Max quoted", "Enterprise")
_BAD_CURRENCIES = (None, True, 840, "usd", "EUR", "", " USD ")


@pytest.mark.parametrize(
    "malformed",
    [
        _old_wire_format(),
        {**_old_wire_format(), "max_agents_per_account": 5},
        {**_current(), "max_digital_employees": 6},
        {key: value for key, value in _current().items() if key != "max_agents_per_account"},
        *({**_current(), "max_agents_per_account": value} for value in _BAD_CAPS),
        {**_current(), "unexpected": True},
        *({key: value for key, value in _current().items() if key != field} for field in _QUOTED_ENUM_FIELDS),
        *({key: value for key, value in _current().items() if key != field} for field in _ADDON_FIELDS),
        *({**_current(), "annual_price_usd": value} for value in _BAD_ANNUAL),
        *({**_current(), "included_agents": value} for value in _BAD_INCLUDED),
        *({**_current(), "additional_agent_monthly_usd": value} for value in _BAD_AGENT_PRICES),
        *({**_current(), field: value} for field in _ADDON_FIELDS for value in _BAD_ADDONS),
        {key: value for key, value in _current().items() if key != "storage_limit_bytes"},
        {key: value for key, value in _current().items() if key != "max_file_size_bytes"},
        {key: value for key, value in _current().items() if key != "daily_messages_per_agent"},
        *({**_current(), "daily_messages_per_agent": value} for value in _BAD_DAILY),
        {**_current(), "daily_messages_per_agent": OrganizationAgentLimit.MAX},
        *({key: value for key, value in _current().items() if key != field} for field in _GUEST_RESOURCE_FIELDS),
        *({**_current(), "storage_limit_bytes": value} for value in _BAD_BYTES),
        *({**_current(), "max_file_size_bytes": value} for value in _BAD_BYTES),
        {**_current(), "storage_limit_bytes": OrganizationFileLimitBytes.MAX},
        {**_current(), "max_file_size_bytes": OrganizationStorageLimitBytes.MAX},
        {key: value for key, value in _current().items() if key != "additional_account_monthly_usd"},
        *({**_current(), "additional_account_monthly_usd": value} for value in _BAD_PRICES),
        {**_current(), "max_accounts": 75},
        {key: value for key, value in _current().items() if key != "name"},
        *({**_current(), "name": value} for value in _BAD_NAMES),
        {key: value for key, value in _current().items() if key != "currency"},
        *({**_current(), "currency": value} for value in _BAD_CURRENCIES),
    ],
    ids=[
        "old-wire-format",
        "old-mixed",
        "current-with-old-field",
        "neither",
        *(f"cap-{value!r}" for value in _BAD_CAPS),
        "extra",
        *(f"no-{field.replace('_', '-')}" for field in _QUOTED_ENUM_FIELDS),
        *(f"no-{field.replace('_', '-')}" for field in _ADDON_FIELDS),
        *(f"annual-price-{value!r}" for value in _BAD_ANNUAL),
        *(f"included-agents-{value!r}" for value in _BAD_INCLUDED),
        *(f"additional-agent-price-{value!r}" for value in _BAD_AGENT_PRICES),
        *(f"{field.replace('_', '-')}-{value!r}" for field in _ADDON_FIELDS for value in _BAD_ADDONS),
        "no-storage",
        "no-file-size",
        "no-daily-messages",
        *(f"daily-messages-{value!r}" for value in _BAD_DAILY),
        "daily-messages-holds-agent-limit",
        *(f"no-{field.replace('_', '-')}" for field in _GUEST_RESOURCE_FIELDS),
        *(f"storage-{value!r}" for value in _BAD_BYTES),
        *(f"file-{value!r}" for value in _BAD_BYTES),
        "storage-holds-file-limit",
        "file-holds-storage-limit",
        "no-additional-account-price",
        *(f"additional-price-{value!r}" for value in _BAD_PRICES),
        "retired-max-accounts",
        "no-name",
        *(f"name-{value!r}" for value in _BAD_NAMES),
        "no-currency",
        *(f"currency-{value!r}" for value in _BAD_CURRENCIES),
    ],
)
def test_only_the_current_shape_with_a_defined_cap_is_accepted(malformed: dict[str, object]):
    with pytest.raises(ValidationError):
        _ = OrganizationPlan.model_validate(malformed)


def test_admission_row_reads_the_current_shape_and_refuses_the_old_wire_format():
    row = {
        "id": uuid4(),
        "email": "buyer@example.com",
        "tier": "max",
        "created_at": datetime.now(UTC),
        "status": "approved",
    }
    current = OrganizationAdmissionRecord.from_row({**row, "plan": _current()})
    assert current.plan == plan_for(OrganizationPlanId.MAX)
    with pytest.raises(ValidationError):
        _ = OrganizationAdmissionRecord.from_row({**row, "plan": _old_wire_format()})
    old_without_storage = {
        key: value
        for key, value in _old_wire_format().items()
        if key not in {"storage_limit_bytes", "max_file_size_bytes"}
    }
    with pytest.raises(ValidationError):
        _ = OrganizationAdmissionRecord.from_row({**row, "plan": old_without_storage})
    without_daily = {key: value for key, value in _current().items() if key != "daily_messages_per_agent"}
    with pytest.raises(ValidationError):
        _ = OrganizationAdmissionRecord.from_row({**row, "plan": without_daily})


def _admission_row(plan: object, tier: OrganizationPlanId = OrganizationPlanId.MAX) -> dict[str, object]:
    return {
        "id": uuid4(),
        "email": "buyer@example.com",
        "tier": tier.value,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "status": "approved",
        "plan": plan,
    }


@pytest.mark.parametrize("tier", list(OrganizationPlanId))
def test_admission_row_decodes_a_complete_stored_snapshot_verbatim(tier: OrganizationPlanId):
    raw = plan_for(tier).model_dump(mode="json")
    raw["monthly_price_usd"] = 1234

    stored = OrganizationAdmissionRecord.from_row(_admission_row(raw, tier))

    assert stored.plan.model_dump(mode="json") == raw


@pytest.mark.parametrize("field", list(OrganizationPlan.model_fields))
def test_admission_row_refuses_a_stored_snapshot_missing_any_field(field: str):
    snapshot = {key: value for key, value in _current().items() if key != field}

    with pytest.raises(ValidationError):
        _ = OrganizationAdmissionRecord.from_row(_admission_row(snapshot))


@pytest.mark.parametrize("field", _GUEST_RESOURCE_FIELDS)
def test_admission_row_keeps_a_stored_guest_resource_over_the_tier_value(field: str):
    pro = plan_for(OrganizationPlanId.PRO).model_dump(mode="json")
    max_plan = plan_for(OrganizationPlanId.MAX).model_dump(mode="json")

    stored = OrganizationAdmissionRecord.from_row(_admission_row({**max_plan, field: pro[field]})).plan.model_dump(
        mode="json"
    )

    assert stored[field] == pro[field] != max_plan[field]
    assert {key: stored[key] for key in _GUEST_RESOURCE_FIELDS if key != field} == {
        key: max_plan[key] for key in _GUEST_RESOURCE_FIELDS if key != field
    }


@pytest.mark.parametrize("field", ["storage_limit_bytes", "max_file_size_bytes"])
@pytest.mark.parametrize("value", [None, 0, 12345])
def test_admission_row_rejects_explicit_invalid_storage_quotas(field: str, value: int | None):
    with pytest.raises(ValidationError):
        _ = OrganizationAdmissionRecord.from_row(_admission_row({**_current(), field: value}))

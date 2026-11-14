"""Per-tier guest limits on creation and on resume, with admission reserving the target."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pywhales.components.container.models import ContainerHostConfig
from pywhales.exceptions import DockerException

from app.dao.agent_workspace_usage_dao import agent_workspace_usage_dao
from app.records.agent import AgentRecord
from app.records.agent_workspace_usage import AgentWorkspaceUsageRecord
from app.records.organization_plan import OrganizationPlanId
from app.services import guest_admission, guest_limits, guest_start
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError
from app.services.guest_limits import GuestLimits, limits_differ, resolve_guest_limits
from app.services.organization_plans import AmbiguousOrganizationTierError
from test_guest_start_replay import env, ready_guest

__all__ = ["env", "ready_guest"]

GIB = 1024**3
TIERS = [
    pytest.param(OrganizationPlanId.PRO, 2 * GIB, 1.5, 1024, 512, id="pro"),
    pytest.param(OrganizationPlanId.MAX, 3 * GIB, 2.0, 1536, 768, id="max"),
    pytest.param(OrganizationPlanId.ULTRA, 4 * GIB, 3.0, 2048, 1024, id="ultra"),
]
MAX_LIMITS = GuestLimits(memory_bytes=3 * GIB, cpus=2.0, cpu_shares=1536, pids_limit=768, tier=OrganizationPlanId.MAX)


def organization_agent(*, system: bool = False, tenant: bool = True) -> AgentRecord:
    return AgentRecord(
        id=uuid4(), name="guest", creator_id=uuid4(), tenant_id=uuid4() if tenant else None, is_system=system
    )


def tier_lookup(monkeypatch, outcome) -> AsyncMock:
    lookup = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    monkeypatch.setattr(guest_limits, "tier_for_tenant", lookup)
    return lookup


def join_organization(monkeypatch, agent: AgentRecord, outcome) -> AsyncMock:
    agent.tenant_id = uuid4()
    monkeypatch.setattr(
        "app.services.enterprise_llm.ensure_agent_company_models", AsyncMock(side_effect=lambda candidate: candidate)
    )
    return tier_lookup(monkeypatch, outcome)


@pytest.mark.parametrize(("tier", "memory", "cpus", "shares", "pids"), TIERS)
async def test_resolve_uses_the_tier_plan_as_plain_numbers(monkeypatch, tier, memory, cpus, shares, pids):
    # Given an organization whose approved admission carries the tier.
    agent = organization_agent()
    lookup = tier_lookup(monkeypatch, tier)
    # When its guest limits are resolved.
    limits = await resolve_guest_limits(agent)
    # Then they are the tier's catalog numbers, as ints and a float rather than enum members.
    assert limits == GuestLimits(memory_bytes=memory, cpus=cpus, cpu_shares=shares, pids_limit=pids, tier=tier)
    assert [type(value) for value in (limits.memory_bytes, limits.cpus, limits.cpu_shares, limits.pids_limit)] == [
        int,
        float,
        int,
        int,
    ]
    assert limits.nano_cpus == round(cpus * 1_000_000_000)
    lookup.assert_awaited_once_with(agent.tenant_id)


@pytest.mark.parametrize("kind", ["no-tenant", "system-agent", "untiered"])
async def test_resolve_falls_back_to_settings_without_cpu_shares(monkeypatch, kind):
    # Given global settings and an agent that is system-owned, tenantless, or in an untiered organization.
    monkeypatch.setattr(guest_limits.settings, "OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", 512)
    monkeypatch.setattr(guest_limits.settings, "OPENCLAW_GUEST_CPUS", 0.5)
    monkeypatch.setattr(guest_limits.settings, "OPENCLAW_GUEST_PIDS_LIMIT", 300)
    agent = organization_agent(system=kind == "system-agent", tenant=kind != "no-tenant")
    lookup = tier_lookup(monkeypatch, None if kind == "untiered" else OrganizationPlanId.ULTRA)
    # When its guest limits are resolved.
    limits = await resolve_guest_limits(agent)
    # Then a system agent never reaches the tier lookup even in a tier-governed system organization, and Docker's
    # default CPU weight is kept.
    assert limits == GuestLimits(memory_bytes=512, cpus=0.5, cpu_shares=None, pids_limit=300, tier=None)
    assert limits.nano_cpus == 500_000_000
    assert limits.cpu_shares_option() == {}
    assert lookup.await_count == (1 if kind == "untiered" else 0)


async def test_resolve_reports_an_ambiguous_tier_as_a_configuration_failure(monkeypatch):
    # Given an organization linked to several approved admissions.
    agent = organization_agent()
    ambiguity = AmbiguousOrganizationTierError(uuid4())
    tier_lookup(monkeypatch, ambiguity)
    # When its guest limits are resolved.
    with pytest.raises(GuestStartPreflightError) as refused:
        await resolve_guest_limits(agent)
    # Then the start fails with a typed refusal instead of escaping as a server error.
    assert (refused.value.code, str(refused.value), refused.value.__cause__) == (
        "configuration_failed",
        "Organization tier is ambiguous",
        ambiguity,
    )


@pytest.mark.parametrize(
    ("observed", "differs"),
    [
        pytest.param({}, False, id="match"),
        pytest.param({"memory": 2 * GIB}, True, id="memory"),
        pytest.param({"memory_swap": 4 * GIB}, True, id="swap"),
        pytest.param({"memory_swap": None}, True, id="swap-unknown"),
        pytest.param({"nano_cpus": 1_000_000_000}, True, id="cpus"),
        pytest.param({"pids_limit": 512}, True, id="pids"),
        pytest.param({"cpu_shares": 1024}, True, id="shares"),
        pytest.param({"cpu_shares": None}, True, id="shares-unknown"),
    ],
)
def test_limits_differ_compares_each_hard_limit(observed, differs):
    # Given a guest created with the Max tier's limits, changed in at most one respect.
    host_config = ContainerHostConfig(
        memory=3 * GIB, memory_swap=3 * GIB, nano_cpus=2_000_000_000, pids_limit=768, cpu_shares=1536
    ).model_copy(update=observed)
    # When it is compared with the Max target.
    # Then any changed limit is a difference.
    assert limits_differ(host_config, MAX_LIMITS) is differs


@pytest.mark.parametrize(
    ("shares", "differs"),
    [
        pytest.param(None, False, id="absent"),
        pytest.param(0, False, id="docker-zero"),
        pytest.param(1024, False, id="docker-default"),
        pytest.param(2048, True, id="former-tier"),
    ],
)
def test_limits_differ_accepts_only_the_default_weight_when_the_target_sets_none(shares, differs):
    # Given a settings-governed target and a guest carrying some CPU weight.
    target = replace(MAX_LIMITS, cpu_shares=None, tier=None)
    host_config = ContainerHostConfig(
        memory=3 * GIB, memory_swap=3 * GIB, nano_cpus=2_000_000_000, pids_limit=768, cpu_shares=shares
    )
    # When compared.
    # Then only a weight other than Docker's default is a difference.
    assert limits_differ(host_config, target) is differs


@pytest.mark.parametrize(("tier", "memory", "cpus", "shares", "pids"), TIERS)
async def test_new_guest_gets_its_tier_limits(env, monkeypatch, tier, memory, cpus, shares, pids):
    # Given an agent of an organization on the tier.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, tier)
    # When its guest is replaced.
    result = await manager.replace_container(agent)
    # Then Docker gets the tier's hard limits with no extra swap, and no new label.
    assert result == agent.container_id
    run = docker.runs[0]
    assert (run["memory"], run["memory_swap"], run["cpus"], run["cpu_shares"], run["pids_limit"]) == (
        memory,
        memory,
        cpus,
        shares,
        pids,
    )
    assert set(run["labels"]) == {"maraclaw.agent_id", "maraclaw.agent_name"}


@pytest.mark.parametrize("kind", ["no-tenant", "system-agent", "untiered"])
async def test_new_guest_of_system_or_untiered_agent_gets_settings_limits(env, monkeypatch, kind):
    # Given an agent that no tier governs, beside a configured default of 20 bytes.
    manager, docker, agent = env
    lookup = join_organization(monkeypatch, agent, None if kind == "untiered" else OrganizationPlanId.ULTRA)
    agent.is_system = kind == "system-agent"
    if kind == "no-tenant":
        agent.tenant_id = None
    # When its guest is replaced.
    await manager.replace_container(agent)
    # Then the global settings apply and Docker keeps its default CPU weight.
    run = docker.runs[0]
    assert (run["memory"], run["memory_swap"], run["cpus"], run["pids_limit"]) == (20, 20, 1.0, 512)
    assert "cpu_shares" not in run
    assert lookup.await_count == (1 if kind == "untiered" else 0)


@pytest.mark.parametrize(
    ("aggregate", "admitted"),
    [pytest.param(4 * GIB, True, id="exact-fit"), pytest.param(4 * GIB - 1, False, id="one-byte-short")],
)
async def test_replacement_admission_reserves_the_tier_target(env, monkeypatch, aggregate, admitted):
    # Given a 1 GiB peer, a Max-tier target of 3 GiB, and a budget around their sum.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    docker.add(replace(agent, id=uuid4(), container_id="other"), memory=GIB)
    monkeypatch.setattr(guest_admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", aggregate)
    # When the guest is replaced.
    if admitted:
        await manager.replace_container(agent)
        # Then the target, not the 20-byte default, is what fits exactly.
        assert docker.runs[0]["memory"] == 3 * GIB
    else:
        with pytest.raises(GuestAdmissionError) as refused:
            await manager.replace_container(agent)
        # Then a single byte over is refused with the old guest untouched.
        assert refused.value.code == "guest_memory_capacity_exceeded"
        assert docker.removed == docker.runs == []
        assert (agent.status, agent.container_id) == ("running", "old")


async def test_ambiguous_tier_refuses_replacement_before_any_docker_mutation(env, monkeypatch):
    # Given an organization linked to several approved admissions.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, AmbiguousOrganizationTierError(uuid4()))
    # When its guest is replaced.
    with pytest.raises(GuestStartPreflightError) as refused:
        await manager.replace_container(agent)
    # Then nothing is pulled, listed, stopped, removed or created, and the runtime record is unchanged.
    assert refused.value.code == "configuration_failed"
    assert docker.pulls == docker.listings == docker.updates == docker.removed == docker.runs == []
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)


@pytest.mark.parametrize(("tier", "memory", "cpus", "shares", "pids"), TIERS)
@pytest.mark.parametrize("state", ["exited", "created"])
async def test_resume_moves_a_stopped_guest_to_its_tier_limits(
    env, monkeypatch, state, tier, memory, cpus, shares, pids
):
    # Given a stopped guest created under other limits (the fake's 20 bytes, 1 CPU, 512 PIDs, default weight).
    manager, docker, agent = env
    join_organization(monkeypatch, agent, tier)
    guest = docker.guests["old"]
    guest.state.status = state
    agent.status = "stopped"
    # When Start resumes it.
    result = await manager.start_container(agent)
    # Then one update brings memory with swap, CPUs, weight and PIDs to the tier before the guest starts.
    assert result == "old"
    assert docker.updates == [
        ("old", {"memory": memory, "memory_swap": memory, "cpus": cpus, "pids_limit": pids, "cpu_shares": shares})
    ]
    host_config = guest.host_config
    assert (host_config.memory, host_config.memory_swap, host_config.nano_cpus) == (memory, memory, round(cpus * 1e9))
    assert (host_config.pids_limit, host_config.cpu_shares) == (pids, shares)
    assert docker.starts == ["old"]
    assert docker.runs == docker.removed == []


@pytest.mark.parametrize("kind", ["no-tenant", "system-agent", "untiered"])
async def test_resume_moves_a_stopped_guest_to_settings_limits_without_cpu_shares(env, monkeypatch, kind):
    # Given a stopped guest at 256 bytes and an agent that no tier governs (default 20 bytes).
    manager, docker, agent = env
    join_organization(monkeypatch, agent, None if kind == "untiered" else OrganizationPlanId.ULTRA)
    agent.is_system = kind == "system-agent"
    if kind == "no-tenant":
        agent.tenant_id = None
    docker.guests["old"].state.status = "exited"
    docker.guests["old"].host_config.memory = docker.guests["old"].host_config.memory_swap = 256
    # When Start resumes it.
    await manager.start_container(agent)
    # Then the update carries the settings values and Docker's default CPU weight.
    assert docker.updates == [
        ("old", {"memory": 20, "memory_swap": 20, "cpus": 1.0, "pids_limit": 512, "cpu_shares": 1024})
    ]
    assert docker.starts == ["old"]


async def test_resume_of_a_guest_that_left_its_tier_drops_the_tier_weight(env, monkeypatch):
    # Given a stopped guest still at the Ultra weight whose organization is no longer tiered.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, None)
    guest = docker.guests["old"]
    guest.state.status = "exited"
    guest.host_config.memory = guest.host_config.memory_swap = 20
    guest.host_config.cpu_shares = 2048
    # When Start resumes it.
    await manager.start_container(agent)
    # Then the only difference, the weight, is reset to Docker's default before the start.
    assert docker.updates == [
        ("old", {"memory": 20, "memory_swap": 20, "cpus": 1.0, "pids_limit": 512, "cpu_shares": 1024})
    ]
    assert guest.host_config.cpu_shares == 1024
    assert docker.starts == ["old"]


@pytest.mark.parametrize("tiered", [False, True], ids=["settings", "tier"])
async def test_resume_with_unchanged_limits_does_not_update(env, monkeypatch, tiered):
    # Given a stopped guest already carrying exactly its target limits.
    manager, docker, agent = env
    guest = docker.guests["old"]
    guest.state.status = "exited"
    if tiered:
        join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
        guest.host_config.memory = guest.host_config.memory_swap = 3 * GIB
        guest.host_config.nano_cpus, guest.host_config.pids_limit, guest.host_config.cpu_shares = (
            2_000_000_000,
            768,
            1536,
        )
    # When Start resumes it.
    result = await manager.start_container(agent)
    # Then it just starts.
    assert result == "old"
    assert docker.updates == []
    assert docker.starts == ["old"]


async def test_resume_updates_when_only_the_cpu_weight_differs(env, monkeypatch):
    # Given a stopped Ultra-tier guest whose other limits match but whose CPU weight is Docker's default.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.ULTRA)
    guest = docker.guests["old"]
    guest.state.status = "exited"
    guest.host_config.memory = guest.host_config.memory_swap = 4 * GIB
    guest.host_config.nano_cpus, guest.host_config.pids_limit = 2_000_000_000, 1024
    # When Start resumes it.
    await manager.start_container(agent)
    # Then the weight alone triggers the update.
    assert [reference for reference, _options in docker.updates] == ["old"]
    assert guest.host_config.cpu_shares == 2048


async def test_running_guest_is_reused_without_any_limit_update(env, monkeypatch):
    # Given a running guest under other limits than its target.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    # When Start reuses it.
    result = await manager.start_container(agent)
    # Then it is neither updated nor restarted.
    assert result == "old"
    assert docker.updates == docker.starts == docker.listings == []
    assert docker.guests["old"].host_config.memory == 20


async def test_failed_update_leaves_the_stopped_guest_unchanged_and_unstarted(env, monkeypatch):
    # Given a stopped guest whose limits need raising and a daemon that refuses the update.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    guest = docker.guests["old"]
    guest.state.status = "exited"
    agent.status = "stopped"
    docker.update_failure = DockerException(["docker", "update"], 1, stderr=b"private daemon context")
    config = manager._agent_dir(agent.id) / "openclaw.json"
    config.write_text("retained config", encoding="utf-8")
    # When Start tries to resume it.
    with pytest.raises(GuestStartPreflightError) as failed:
        await manager.start_container(agent)
    # Then the failure is a typed runtime refusal without daemon details, and nothing was started or rewritten.
    assert (failed.value.code, str(failed.value)) == ("runtime_failed", "Guest limits could not be updated")
    assert "private" not in str(failed.value)
    assert docker.starts == docker.runs == docker.removed == []
    assert (guest.state.status, guest.host_config.memory) == ("exited", 20)
    assert (agent.container_id, agent.status, agent.container_port) == ("old", "stopped", 18888)
    assert config.read_text(encoding="utf-8") == "retained config"


async def test_ambiguous_tier_refuses_resume_before_any_docker_mutation(env, monkeypatch):
    # Given a stopped guest of an organization linked to several approved admissions.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, AmbiguousOrganizationTierError(uuid4()))
    docker.guests["old"].state.status = "exited"
    # When Start tries to resume it.
    with pytest.raises(GuestStartPreflightError) as refused:
        await manager.start_container(agent)
    # Then the typed refusal precedes registration, admission, update and start.
    assert refused.value.code == "configuration_failed"
    assert docker.listings == docker.updates == docker.starts == docker.runs == docker.removed == []
    assert docker.guests["old"].state.status == "exited"
    manager._prepare_tavily_proxy_token.assert_not_awaited()


GUEST_IMAGE = f"sha256:{'a' * 64}"
WORKSPACE_ALLOWANCE = 5 * GIB // 2


def workspace_usage(monkeypatch, agent: AgentRecord, *, files: int, layer: int, over: bool = True) -> AsyncMock:
    row = AgentWorkspaceUsageRecord(
        agent.id, files, layer, WORKSPACE_ALLOWANCE, over_since=datetime.now(UTC) if over else None
    )
    read = AsyncMock(return_value=row)
    monkeypatch.setattr(agent_workspace_usage_dao, "get", read)
    return read


def watch_remeasure(monkeypatch, docker) -> AsyncMock:
    remeasure = AsyncMock(side_effect=lambda _agent: docker.events.append("remeasure"))
    monkeypatch.setattr(guest_start, "remeasure_after_recreation", remeasure)
    return remeasure


@pytest.mark.parametrize("state", ["exited", "created"])
async def test_a_stopped_guest_whose_layer_alone_blocks_the_workspace_is_recreated(env, monkeypatch, state):
    # Given a stopped Max-tier guest whose files fit the allowance but whose container layer pushes it over.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    guest = docker.guests["old"]
    guest.state.status, guest.image = state, GUEST_IMAGE
    agent.status = "stopped"
    read = workspace_usage(monkeypatch, agent, files=GIB, layer=2 * GIB)
    remeasure = watch_remeasure(monkeypatch, docker)
    operation = uuid4()
    token = guest_start.start_operation_id.set(operation)
    # When Start runs under a durable start operation.
    try:
        result = await manager.start_container(agent)
    finally:
        guest_start.start_operation_id.reset(token)
    # Then the old guest is removed without being updated or started, and a new one is created on the same image
    # under the tier's limits and the operation's label.
    assert result == agent.container_id == "created-1"
    assert docker.removed == ["old"]
    assert docker.updates == docker.starts == []
    assert docker.images == [GUEST_IMAGE]
    run = docker.runs[0]
    assert (run["memory"], run["memory_swap"], run["cpus"], run["pids_limit"], run["cpu_shares"]) == (
        3 * GIB,
        3 * GIB,
        2.0,
        768,
        1536,
    )
    assert run["labels"]["maraclaw.start_operation"] == str(operation)
    # And the new guest is measured right after creation, then must pass readiness before `running` is published.
    read.assert_awaited_once_with(agent.id)
    remeasure.assert_awaited_once_with(agent)
    assert docker.events == ["run", "remeasure"]
    assert docker.probes == ["created-1"] * 2
    assert agent.status == "running"


async def test_a_stopped_guest_whose_files_exceed_the_allowance_is_resumed(env, monkeypatch):
    # Given a stopped guest whose host files alone are over the allowance.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    docker.guests["old"].state.status = "exited"
    workspace_usage(monkeypatch, agent, files=3 * GIB, layer=GIB)
    remeasure = watch_remeasure(monkeypatch, docker)
    # When Start runs.
    result = await manager.start_container(agent)
    # Then the same guest resumes: deleting files is the only remedy, a new container would not help.
    assert result == "old"
    assert docker.starts == ["old"]
    assert docker.runs == docker.removed == []
    remeasure.assert_not_awaited()


@pytest.mark.parametrize("measured", [True, False], ids=["under-allowance", "unmeasured"])
async def test_a_stopped_guest_that_is_not_blocked_is_resumed(env, monkeypatch, measured):
    # Given a stopped guest whose agent is under its allowance, or has not been measured yet.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    docker.guests["old"].state.status = "exited"
    if measured:
        _ = workspace_usage(monkeypatch, agent, files=GIB, layer=GIB, over=False)
    remeasure = watch_remeasure(monkeypatch, docker)
    # When Start runs.
    result = await manager.start_container(agent)
    # Then the same guest resumes.
    assert result == "old"
    assert docker.starts == ["old"]
    assert docker.runs == docker.removed == []
    remeasure.assert_not_awaited()


async def test_a_running_guest_is_reused_untouched_even_when_its_layer_blocks_the_workspace(env, monkeypatch):
    # Given a running guest whose layer alone keeps its agent over the allowance.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    read = workspace_usage(monkeypatch, agent, files=GIB, layer=2 * GIB)
    remeasure = watch_remeasure(monkeypatch, docker)
    # When Start runs.
    result = await manager.start_container(agent)
    # Then it is reused as is: the usage is not even read, and nothing is updated, started, removed or created.
    assert result == "old"
    read.assert_not_awaited()
    assert docker.updates == docker.starts == docker.runs == docker.removed == []
    remeasure.assert_not_awaited()


async def test_a_stopped_guest_without_the_host_state_mount_is_resumed_not_recreated(env, monkeypatch):
    # Given a blocked, stopped guest that does not carry the host state mount, so its state may live in its layer.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    guest = docker.guests["old"]
    guest.state.status, guest.mounts = "exited", []
    workspace_usage(monkeypatch, agent, files=GIB, layer=2 * GIB)
    remeasure = watch_remeasure(monkeypatch, docker)
    # When Start runs.
    result = await manager.start_container(agent)
    # Then it resumes rather than risk the state.
    assert result == "old"
    assert docker.starts == ["old"]
    assert docker.runs == docker.removed == []
    remeasure.assert_not_awaited()


async def test_a_refused_recreation_leaves_the_stopped_guest_intact(env, monkeypatch):
    # Given a blocked, stopped guest whose image the daemon no longer has (a local id is never pulled).
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    guest = docker.guests["old"]
    guest.state.status, guest.image = "exited", GUEST_IMAGE
    agent.status = "stopped"
    docker.image_present = False
    workspace_usage(monkeypatch, agent, files=GIB, layer=2 * GIB)
    remeasure = watch_remeasure(monkeypatch, docker)
    # When Start tries to recreate it.
    with pytest.raises(GuestStartPreflightError) as refused:
        await manager.start_container(agent)
    # Then the replacement path's own refusal applies before anything destructive, and nothing is measured.
    assert refused.value.code == "runtime_failed"
    assert docker.removed == docker.runs == docker.updates == docker.starts == docker.pulls == []
    assert guest.state.status == "exited"
    assert (agent.container_id, agent.status, agent.container_port) == ("old", "stopped", 18888)
    remeasure.assert_not_awaited()


async def test_an_unready_recreated_guest_is_quiesced_like_any_new_writer(env, monkeypatch):
    # Given a recreation whose new guest never becomes ready.
    manager, docker, agent = env
    join_organization(monkeypatch, agent, OrganizationPlanId.MAX)
    guest = docker.guests["old"]
    guest.state.status, guest.image = "exited", GUEST_IMAGE
    workspace_usage(monkeypatch, agent, files=GIB, layer=2 * GIB)
    _ = watch_remeasure(monkeypatch, docker)

    async def unready(_client, _reference):
        return False

    monkeypatch.setattr(guest_start, "guest_ready", unready)
    # When Start waits for readiness.
    with pytest.raises(GuestStartPreflightError) as failure:
        await manager.start_container(agent)
    # Then the new writer is stopped, not orphaned, and the failure is the usual readiness one.
    assert failure.value.code == "readiness_failed"
    assert docker.removed == ["old"]
    assert docker.guests["created-1"].state.status == "exited"
    assert agent.status == "error"

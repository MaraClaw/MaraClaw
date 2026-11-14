"""Hard limits of one OpenClaw guest container: tier resolution, Docker options and drift detection."""

from dataclasses import dataclass
from typing import TypedDict

from pywhales.components.container.models import ContainerHostConfig

from app.config import get_settings
from app.records.agent import AgentRecord
from app.records.organization_plan import OrganizationPlanId
from app.services.guest_admission import GuestStartPreflightError
from app.services.organization_plans import AmbiguousOrganizationTierError, plan_for, tier_for_tenant

settings = get_settings()

_NANO_CPUS_PER_CPU = 1_000_000_000
DOCKER_DEFAULT_CPU_SHARES = 1024


class CpuSharesOption(TypedDict, total=False):
    """Docker's ``cpu_shares`` keyword, present only when a tier sets it (otherwise Docker's default applies)."""

    cpu_shares: int


@dataclass(frozen=True, slots=True)
class GuestLimits:
    memory_bytes: int
    cpus: float
    cpu_shares: int | None
    pids_limit: int
    tier: OrganizationPlanId | None

    @property
    def nano_cpus(self) -> int:
        """Docker's integer CPU ceiling, as reported by ``HostConfig.NanoCpus``."""
        return round(self.cpus * _NANO_CPUS_PER_CPU)

    def cpu_shares_option(self) -> CpuSharesOption:
        return {} if self.cpu_shares is None else {"cpu_shares": self.cpu_shares}

    @property
    def update_cpu_shares(self) -> int:
        """Weight for ``docker update``: a guest leaving a tier must drop that tier's weight back to Docker's default."""
        return DOCKER_DEFAULT_CPU_SHARES if self.cpu_shares is None else self.cpu_shares


async def resolve_guest_limits(agent: AgentRecord) -> GuestLimits:
    """Tier limits of the agent's organization; system agents and untiered organizations use the global settings.

    ``is_system`` is checked first: the system organizations are tier-governed, but their system agents are not.
    """
    if not agent.is_system and agent.tenant_id is not None:
        try:
            tier = await tier_for_tenant(agent.tenant_id)
        except AmbiguousOrganizationTierError as exc:
            raise GuestStartPreflightError("Organization tier is ambiguous", code="configuration_failed") from exc
        if tier is not None:
            plan = plan_for(tier)
            return GuestLimits(
                memory_bytes=int(plan.guest_memory_bytes),
                cpus=int(plan.guest_millicpus) / 1000,
                cpu_shares=int(plan.guest_cpu_shares),
                pids_limit=int(plan.guest_pids_limit),
                tier=tier,
            )
    return GuestLimits(
        memory_bytes=settings.OPENCLAW_GUEST_MEMORY_LIMIT_BYTES,
        cpus=settings.OPENCLAW_GUEST_CPUS,
        cpu_shares=None,
        pids_limit=settings.OPENCLAW_GUEST_PIDS_LIMIT,
        tier=None,
    )


def limits_differ(host_config: ContainerHostConfig, limits: GuestLimits) -> bool:
    """Whether an inspected guest's hard limits differ from the target.

    Swap must equal memory (no extra swap allowance). A target without CPU shares accepts Docker's default weight
    (reported as 0, 1024 or absent) and nothing else, so a weight left by a former tier is a difference.
    """
    if limits.cpu_shares is None:
        shares_differ = host_config.cpu_shares not in (None, 0, DOCKER_DEFAULT_CPU_SHARES)
    else:
        shares_differ = host_config.cpu_shares != limits.cpu_shares
    return (
        host_config.memory != limits.memory_bytes
        or host_config.memory_swap != limits.memory_bytes
        or host_config.nano_cpus != limits.nano_cpus
        or host_config.pids_limit != limits.pids_limit
        or shares_differ
    )

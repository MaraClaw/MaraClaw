"""Dictionary contracts for handover and agent observability."""

from typing import Literal, TypedDict

from app.core.domain.agents import AgentStatus
from app.services.agent_manager import ContainerStatus, InspectedContainerStatus


class AgentHandoverResponse(TypedDict):
    status: Literal["transferred"]
    agent_name: str
    new_creator: str


class AgentTokenMetrics(TypedDict):
    used_today: int
    used_month: int
    used_total: int
    cache_read_today: int
    cache_read_month: int
    cache_read_total: int
    cache_creation_today: int
    cache_creation_month: int
    cache_creation_total: int
    cache_hit_rate_today: float
    cache_hit_rate_month: float
    cache_hit_rate_total: float
    limit_day: int | None
    limit_month: int | None


class AgentTaskMetrics(TypedDict):
    total: int
    done: int
    pending: int
    completion_rate: float


class AgentApprovalMetrics(TypedDict):
    total: int
    pending: int


class AgentActivityMetrics(TypedDict):
    actions_last_24h: int


class AgentMetricsResponse(TypedDict):
    agent_id: str
    agent_name: str
    status: AgentStatus
    container: ContainerStatus | InspectedContainerStatus
    tokens: AgentTokenMetrics
    tasks: AgentTaskMetrics
    approvals: AgentApprovalMetrics
    activity: AgentActivityMetrics

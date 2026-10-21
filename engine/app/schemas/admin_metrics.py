from typing_extensions import TypedDict


class AdminTimeseriesPoint(TypedDict):
    date: str
    new_companies: int
    total_companies: int
    new_users: int
    total_users: int
    new_tokens: int
    total_tokens: int
    new_cache_read_tokens: int
    total_cache_read_tokens: int
    cache_hit_rate: float
    new_sessions: int
    total_sessions: int
    dau: int
    wau: int
    mau: int


class AdminCompanyLeaderboardEntry(TypedDict):
    name: str
    tokens: int
    cache_read_tokens: int
    cache_hit_rate: float


class AdminAgentLeaderboardEntry(AdminCompanyLeaderboardEntry):
    company: str


class AdminLeaderboardsResponse(TypedDict):
    top_companies: list[AdminCompanyLeaderboardEntry]
    top_agents: list[AdminAgentLeaderboardEntry]


class AdminChannelDistributionEntry(TypedDict):
    channel: str
    count: int


class AdminToolCategoryEntry(TypedDict):
    category: str
    count: int


class AdminChurnWarning(TypedDict):
    name: str
    total_tokens: int
    last_active: str | None
    days_inactive: int | None


class AdminEnhancedMetricsResponse(TypedDict):
    avg_tokens_per_session_30d: int
    retention_rate_7d: float
    last_week_active_companies: int
    retained_companies: int
    channel_distribution: list[AdminChannelDistributionEntry]
    tool_category_top10: list[AdminToolCategoryEntry]
    churn_warnings: list[AdminChurnWarning]

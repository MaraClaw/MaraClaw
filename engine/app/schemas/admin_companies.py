"""Platform-admin company wire models: picker options, stats rows, keyset pages and agent section visibility."""

import uuid
from datetime import datetime

from pydantic import BaseModel, computed_field

from app.services.tenant_lifecycle import tenant_can_be_disabled


class CompanyOption(BaseModel):
    """Stats-free company row for pickers."""

    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    sso_enabled: bool = False
    sso_domain: str | None = None
    is_system: bool = False
    is_default_end_user_org: bool = False
    created_at: datetime | None = None

    @computed_field
    @property
    def can_disable(self) -> bool:
        return tenant_can_be_disabled(self)


class CompanyStats(CompanyOption):
    user_count: int = 0
    active_user_count: int = 0
    inactive_user_count: int = 0
    agent_count: int = 0
    agent_running_count: int = 0
    total_tokens: int = 0
    cache_read_tokens_total: int = 0
    org_admin_email: str | None = None


class CompanyPageOut(BaseModel):
    items: list[CompanyStats]
    next_cursor: str | None


class CompanyAgentSections(BaseModel):
    """Agent page sections: every canonical id, and the ones hidden from members of the company."""

    sections: list[str]
    hidden_sections: list[str]


class CompanyAgentSectionsUpdate(BaseModel):
    """Replacement hidden list; duplicates collapse and the stored order is canonical."""

    hidden_sections: list[str]

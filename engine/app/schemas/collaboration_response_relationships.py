"""Dictionary contracts for human and agent relationships."""

from typing import Literal, TypedDict

from app.core.permissions import RelationshipStatus


class CollaborationAcknowledgement(TypedDict):
    status: Literal["ok"]


class HumanRelationshipCandidate(TypedDict):
    id: str
    name: str | None
    email: str | None
    title: str | None
    department_path: str | None
    avatar_url: str | None
    external_id: str | None
    provider_id: str | None
    provider_name: str | None
    provider_type: str | None
    user_id: str | None
    is_platform_user: bool
    platform_access_level: str | None


class RelationshipMemberSummary(TypedDict):
    name: str
    title: str
    department_path: str
    avatar_url: str | None
    email: str | None
    provider_name: str | None
    provider_type: str | None
    user_id: str | None
    is_platform_user: bool


class HumanRelationshipResponse(RelationshipStatus):
    id: str
    member_id: str
    relation: str
    relation_label: str
    description: str
    member: RelationshipMemberSummary | None


class RelationshipAgentSummary(TypedDict):
    id: str
    name: str
    role_description: str
    avatar_url: str
    access_mode: str


class VisibleRelationshipAgent(RelationshipAgentSummary):
    creator_id: str
    can_manage: bool


class AgentRelationshipResponse(RelationshipStatus):
    id: str
    target_agent_id: str
    relation: str
    relation_label: str
    description: str
    target_agent: RelationshipAgentSummary | None

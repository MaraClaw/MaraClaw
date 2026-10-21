from typing import TypedDict
from uuid import UUID


class AgentNameAvatarProjection(TypedDict):
    id: UUID
    name: str
    avatar_url: str | None


class UserNameAvatarProjection(TypedDict):
    id: UUID
    display_name: str
    avatar_url: str | None

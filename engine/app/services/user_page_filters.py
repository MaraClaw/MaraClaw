"""Eligibility and exclusion filters for the company users page, and the cursor digest that binds them."""

import json
from enum import StrEnum
from hashlib import sha256
from typing import assert_never
from uuid import UUID

from app.dao.user_dao import UserPageFilter
from app.services.agent_ownership_transfer import END_USER_ROLES


class UserPageEligibility(StrEnum):
    """Server-side filters. AGENT_TRANSFER mirrors `_check_parties`: end-user role, active membership and identity."""

    AGENT_TRANSFER = "agent_transfer"


def user_page_filter(eligible_for: UserPageEligibility | None, exclude_user_id: UUID | None) -> UserPageFilter | None:
    """Return the DAO filter, or None when the request is unfiltered."""
    if eligible_for is None:
        return None if exclude_user_id is None else UserPageFilter(exclude_user_id=exclude_user_id)
    match eligible_for:
        case UserPageEligibility.AGENT_TRANSFER:
            return UserPageFilter(eligible_roles=tuple(sorted(END_USER_ROLES)), exclude_user_id=exclude_user_id)
        case unreachable:
            assert_never(unreachable)


def user_page_digest(search: str, eligible_for: UserPageEligibility | None, exclude_user_id: UUID | None) -> str:
    """Digest of the page scope; unfiltered requests keep `sha256(search)` so existing cursors stay valid."""
    scope: dict[str, str] = {}
    if eligible_for is not None:
        scope["eligible_for"] = eligible_for.value
    if exclude_user_id is not None:
        scope["exclude_user_id"] = str(exclude_user_id)
    if not scope:
        return sha256(search.encode()).hexdigest()
    return sha256(json.dumps({"search": search, **scope}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

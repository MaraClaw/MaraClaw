"""Typed durable cache invalidation intent and fenced publication claims."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import TypeAdapter


class CacheInvalidationKind(StrEnum):
    ACL_BUMP = "acl_bump"
    ACL_DROP = "acl_drop"
    VERSION = "version"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class CacheInvalidation:
    key: str
    kind: CacheInvalidationKind


@dataclass(frozen=True, slots=True)
class CacheInvalidationClaim:
    key: str
    kind: CacheInvalidationKind
    revision: UUID
    lease_token: UUID
    attempts: int

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> CacheInvalidationClaim:
        return _CLAIM_ADAPTER.validate_python(dict(row))


_CLAIM_ADAPTER: TypeAdapter[CacheInvalidationClaim] = TypeAdapter(CacheInvalidationClaim)

from dataclasses import dataclass
from uuid import UUID

from app.services.storage_runtime.quota_errors import ManagedKeyError
from app.services.storage_runtime.utils import normalize_storage_key


@dataclass(frozen=True, slots=True)
class ManagedKey:
    """A logical storage key under one agent's exclusively owned prefix: ``<agent UUID>/<relative>``."""

    agent_id: UUID
    relative: str


def parse_managed_key(key: str) -> ManagedKey | None:
    """The agent a key belongs to, or ``None`` for non-agent keys such as ``enterprise_info_<tenant>``.

    A first segment that parses as a UUID but is not its canonical lowercase form would address a different
    provider object than the ledger row it maps to, so it fails closed instead of being aliased.
    """
    head, _, relative = normalize_storage_key(key).partition("/")
    try:
        agent_id = UUID(head)
    except ValueError:
        return None
    if str(agent_id) != head:
        raise ManagedKeyError
    return ManagedKey(agent_id, relative)

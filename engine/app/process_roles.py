"""Dependency-free PROCESS_ROLE boundary shared by Python and the entrypoint."""

import os
import sys
from dataclasses import dataclass
from typing import override

VALID_ROLES = frozenset({"all", "bootstrap", "api", "worker", "connector"})


@dataclass(frozen=True, slots=True)
class InvalidProcessRoleError(ValueError):
    unknown: frozenset[str]

    @override
    def __str__(self) -> str:
        return f"Unknown PROCESS_ROLE: {','.join(sorted(self.unknown))}; allowed: {','.join(sorted(VALID_ROLES))}"


def parse_process_roles(raw: str | None) -> frozenset[str]:
    roles = frozenset(part.strip().lower() for part in (raw or "").split(",") if part.strip()) or frozenset({"all"})
    unknown = roles - VALID_ROLES
    if unknown:
        raise InvalidProcessRoleError(unknown)
    return frozenset({"all"}) if "all" in roles else roles


def role_enabled(roles: frozenset[str], role: str) -> bool:
    return "all" in roles or role in roles


def main() -> int:
    try:
        roles = parse_process_roles(os.environ.get("PROCESS_ROLE"))
    except InvalidProcessRoleError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(",".join(sorted(roles)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

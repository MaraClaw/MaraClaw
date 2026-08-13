"""Report the source selected by the AgentBay configuration resolver.

Usage:
    uv run python check_agentbay_config.py [agent_uuid]
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Sequence

import anyio

from app.services.agentbay_config import AgentBayConfigSource, resolve_agentbay_config

_USAGE = "usage: check_agentbay_config.py [agent_uuid]"


async def run(arguments: Sequence[str]) -> int:
    """Resolve configuration for an optional agent and return a process exit code."""
    match tuple(arguments):
        case ():
            agent_id = None
        case ("--help" | "-h",):
            print(_USAGE)
            return 0
        case (agent_id_text,):
            try:
                agent_id = uuid.UUID(agent_id_text)
            except ValueError:
                print(f"Invalid agent UUID: {agent_id_text}", file=sys.stderr)
                return 2
        case _:
            print(_USAGE, file=sys.stderr)
            return 2

    resolution = await resolve_agentbay_config(agent_id, None)
    _report_source(resolution.source)
    return 0 if resolution.api_key is not None else 1


def _report_source(source: AgentBayConfigSource) -> None:
    print(f"AgentBay configuration source: {source.value}")


if __name__ == "__main__":
    raise SystemExit(anyio.run(run, sys.argv[1:]))

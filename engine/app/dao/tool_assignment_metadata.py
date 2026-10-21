"""Joined catalog metadata accompanying an agent-tool assignment."""

from typing import TypedDict


class ToolAssignmentMetadata(TypedDict):
    """Fixed join columns with payloads normalized by the runtime consumer."""

    config: object
    config_schema: object
    source: str
    name: str

from __future__ import annotations

from typing import cast


def _agentbay_response_text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _agentbay_response_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []

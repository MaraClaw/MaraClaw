"""Numeric log levels used by the process logger."""

from __future__ import annotations

TRACE = 5
DEBUG = 10
INFO = 20
SUCCESS = 25
WARNING = 30
ERROR = 40
CRITICAL = 50

_LEVEL_BY_NAME: dict[str, int] = {
    "TRACE": TRACE,
    "DEBUG": DEBUG,
    "INFO": INFO,
    "SUCCESS": SUCCESS,
    "WARNING": WARNING,
    "WARN": WARNING,
    "ERROR": ERROR,
    "CRITICAL": CRITICAL,
    "FATAL": CRITICAL,
}

_NAME_BY_LEVEL: dict[int, str] = {
    TRACE: "TRACE",
    DEBUG: "DEBUG",
    INFO: "INFO",
    SUCCESS: "SUCCESS",
    WARNING: "WARNING",
    ERROR: "ERROR",
    CRITICAL: "CRITICAL",
}


def coerce_level(level: int | str) -> int:
    """Convert a name or number into a numeric level."""
    if isinstance(level, int):
        return level
    try:
        return _LEVEL_BY_NAME[level.upper()]
    except KeyError:
        return INFO


def level_name(level: int | str) -> str:
    """Return the canonical name for a level."""
    if isinstance(level, str):
        return _NAME_BY_LEVEL.get(coerce_level(level), level.upper())
    return _NAME_BY_LEVEL.get(level, f"LEVEL{level}")

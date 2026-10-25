"""Take Control public environment and endpoint results."""

from enum import StrEnum


class ControlEnvironment(StrEnum):
    BROWSER = "browser"
    COMPUTER = "computer"
    CODE = "code"


class ControlStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    ALREADY_LOCKED = "already_locked"
    LOCKED = "locked"
    UNLOCKED = "unlocked"

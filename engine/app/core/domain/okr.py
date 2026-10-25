"""Key-result progress classification."""

from enum import StrEnum


class KeyResultStatus(StrEnum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BEHIND = "behind"
    COMPLETED = "completed"

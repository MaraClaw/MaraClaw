from typing import Literal, NotRequired

from typing_extensions import TypedDict


class OKRTriggerCronConfig(TypedDict):
    expr: str


class DesiredOKRTriggerSpec(TypedDict):
    is_enabled: bool
    config: NotRequired[OKRTriggerCronConfig]
    reason: NotRequired[str]


class DuplicateIdentityConflict(TypedDict):
    type: Literal["email", "mobile"]
    scope: Literal["global"]
    message: str


class DuplicateIdentityResult(TypedDict):
    has_conflict: bool
    conflicts: list[DuplicateIdentityConflict]

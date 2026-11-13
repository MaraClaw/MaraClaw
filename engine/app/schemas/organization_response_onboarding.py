from typing_extensions import TypedDict


class OnboardingStatusPayload(TypedDict):
    exists: bool
    status: str
    current_step: str
    entry_mode: str | None
    personal_assistant_agent_id: str | None
    completed_at: str | None

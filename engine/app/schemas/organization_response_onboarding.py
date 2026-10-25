from typing import Literal

from typing_extensions import TypedDict


class OnboardingStatusPayload(TypedDict):
    exists: bool
    status: str
    current_step: str
    entry_mode: str | None
    personal_assistant_agent_id: str | None
    completed_at: str | None


class PersonalAssistantPayload(TypedDict):
    id: str
    name: str


class PersonalAssistantCreatedPayload(PersonalAssistantPayload):
    api_key: str


class PersonalAssistantStarted(TypedDict):
    status: Literal["started"]


class PersonalAssistantStartFailed(TypedDict):
    status: Literal["failed"]
    code: str
    message: str


type PersonalAssistantStart = PersonalAssistantStarted | PersonalAssistantStartFailed


class PersonalAssistantResponse(TypedDict):
    agent: PersonalAssistantPayload | PersonalAssistantCreatedPayload
    onboarding: OnboardingStatusPayload
    start: PersonalAssistantStart

import pytest
from pydantic import ValidationError

from app.api.agentbay_control import UnlockRequest
from app.schemas.agent_credential import AgentCredentialUpdate


def test_edit_requires_positive_expected_revision() -> None:
    for payload in ({}, {"expected_revision": 0}, {"expected_revision": -1}):
        with pytest.raises(ValidationError):
            AgentCredentialUpdate.model_validate(payload)


def test_capture_requires_explicit_creation_or_revision_bound_replacement() -> None:
    with pytest.raises(ValidationError):
        UnlockRequest(session_id="chat", platform_hint="portal.example")

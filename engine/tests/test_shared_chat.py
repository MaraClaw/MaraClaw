from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from app.records.chat import ChatSessionRecord
from app.services.shared_chat import is_shared_session, shared_session_id


def test_shared_identity_is_agent_scoped_and_stable():
    agent_id = UUID("700ec18f-375e-46ec-ac71-9e77910cf978")

    session_id = shared_session_id(agent_id)

    assert session_id == shared_session_id(agent_id)
    assert session_id.version == 5
    assert session_id != shared_session_id(uuid4())


@pytest.mark.parametrize("changed", ["id", "agent_id", "source_channel"])
def test_shared_marker_requires_both_identity_and_channel(changed):
    agent_id = uuid4()
    session = ChatSessionRecord(
        id=shared_session_id(agent_id), agent_id=agent_id, user_id=uuid4(), source_channel="shared"
    )
    corrupted = replace(session, **{changed: "web" if changed == "source_channel" else uuid4()})

    assert is_shared_session(session)
    assert not is_shared_session(corrupted)

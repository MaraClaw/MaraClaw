"""One freshly authorized Personal WhatsApp original, bound to the fences every batch member must share."""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from fastapi import HTTPException

from app.records.agent import AgentRecord
from app.records.personal_burst import PersonalBurstScope
from app.records.personal_burst_input import ReceiptKey
from app.records.user import UserRecord
from app.records.whatsapp_personal import PersonalRoute, PersonalRouteError, PersonalSource

# A neutral reply-session title: the session title is cut from the first message and a rejected body must not persist.
PERSONAL_REPLY_TITLE: Final = "WhatsApp"


def personal_route(conversation_id: str, source: PersonalSource) -> PersonalRoute:
    """Decode the reply route and require it to carry exactly the socket's fences."""
    try:
        route = PersonalRoute.from_external_id(conversation_id)
    except PersonalRouteError:
        raise HTTPException(403, "Invalid personal route") from None
    if (route.config_id, route.auth_epoch, route.generation, route.account_id) != (
        source.config_id,
        source.auth_epoch,
        source.generation,
        source.account_id,
    ):
        raise HTTPException(403, "Personal route does not match its source")
    return route


@dataclass(frozen=True, slots=True)
class PersonalOriginal:
    agent: AgentRecord
    sender: UserRecord
    receipt: ReceiptKey
    text: str
    route: PersonalRoute
    shared_session_id: UUID
    reply_session_id: UUID

    @property
    def text_bytes(self) -> int:
        return len(self.text.encode())

    @property
    def scope(self) -> PersonalBurstScope:
        if self.agent.tenant_id is None:
            raise HTTPException(403, "Agent is unavailable")
        return PersonalBurstScope(
            agent_id=self.agent.id,
            tenant_id=self.agent.tenant_id,
            shared_session_id=self.shared_session_id,
            reply_session_id=self.reply_session_id,
            sender_user_id=self.sender.id,
            config_id=self.route.config_id,
            account_id=self.route.account_id,
            auth_epoch=self.route.auth_epoch,
            generation=self.route.generation,
            peer_id=self.route.self_jid,
        )

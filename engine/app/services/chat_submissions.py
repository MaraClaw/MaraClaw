import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord
from app.records.user import UserRecord
from app.services.llm.types import OpenAIMessage
from app.services.openclaw_routing import enqueue_prepared_openclaw_message, prepare_openclaw_message
from app.services.quota_guard import charge_conversation_quota, check_agent_expired


class ChatSubmission(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    client_submission_id: UUID
    content: str
    display_content: str = ""
    file_name: str = ""

    def fingerprint(self) -> str:
        canonical = json.dumps(
            [self.content, self.display_content, self.file_name], ensure_ascii=True, separators=(",", ":")
        )
        return "v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def saved_content(self) -> str:
        text = self.content if "[image_data:" in self.content else self.display_content or self.content
        return f"[file:{self.file_name}]\n{text}" if self.file_name else text


class SubmissionConflictError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AcceptedSubmission:
    message: ChatMessageRecord
    replayed: bool


def _replay(existing: ChatMessageRecord, candidate: ChatMessageRecord) -> AcceptedSubmission:
    if existing.submission_fingerprint != candidate.submission_fingerprint:
        raise SubmissionConflictError
    return AcceptedSubmission(existing, replayed=True)


async def accept_submission(
    submission: ChatSubmission,
    *,
    user: UserRecord,
    agent: AgentRecord,
    session_id: UUID,
    history: list[OpenAIMessage],
) -> AcceptedSubmission:
    candidate = ChatMessageRecord(
        id=uuid4(),
        user_id=user.id,
        agent_id=agent.id,
        role="user",
        content=submission.saved_content(),
        conversation_id=str(session_id),
        client_submission_id=submission.client_submission_id,
        submission_fingerprint=submission.fingerprint(),
    )
    async with connection_ctx():
        session = await chat_session_dao.lock_for_agent(session_id, agent.id)
        if session is None:
            raise HTTPException(403, "Session unavailable")
        if session.source_channel != "agent" and session.user_id != user.id:
            raise HTTPException(403, "Not authorized for this session")
        existing = await chat_message_dao.find_submission(candidate)
        if existing is not None:
            return _replay(existing, candidate)

        await check_agent_expired(agent.id)
        prepared = await prepare_openclaw_message(agent=agent, content=submission.content, history=history)
        reserved = await chat_message_dao.reserve_submission(candidate)
        if reserved is None:
            existing = await chat_message_dao.find_submission(candidate)
            if existing is None:
                raise SubmissionConflictError
            return _replay(existing, candidate)
        await charge_conversation_quota(user)
        title = (submission.display_content or submission.content).replace("[图片] ", "📷 ")
        title = title.replace("[image_data:", "").strip()
        if submission.file_name and not title:
            title = f"📎 {submission.file_name}"
        updates: dict[str, object] = {"last_message_at": datetime.now(UTC)}
        if not history and session.title.startswith("Session "):
            updates["title"] = title[:40] if title else submission.content[:40]
        _ = await chat_session_dao.update(db_obj=session, obj_in=updates)
        _ = await enqueue_prepared_openclaw_message(
            prepared, sender_user_id=user.id, conversation_id=str(session_id), deduplicate=False
        )
    return AcceptedSubmission(reserved, replayed=False)

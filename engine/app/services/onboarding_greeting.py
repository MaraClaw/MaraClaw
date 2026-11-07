"""Native identity from durable records: the first-run greeting request and the per-turn identity context."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from app.core.logging import logger
from app.dao.user_dao import user_dao
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime import StorageReadLimitExceededError

_SOUL_LIMIT: Final = 6000
_SOUL_READ_BYTES: Final = 100_000
_NAME_LIMIT: Final = 200

_GREETING_REQUEST: Final = """\
This is the platform's automatic first-run greeting. The person just opened your chat and typed nothing.

Already established. These facts override any BOOTSTRAP, IDENTITY or USER file or upstream onboarding step that \
asks for names:
- Your name is {agent_name}. Introduce yourself by it.
- The person you are greeting is {user_line}.
Never ask either of them to name you or themselves. They are not necessarily the person who created you.

Your soul (personality, voice and role):
<soul>
{soul}
</soul>

Write one greeting of about 80 words:
- Greet them and introduce yourself by name, in the voice and personality of your soul. Make it warm and distinctive.
- Name 1-2 concrete ways you can help, grounded in the role and responsibilities in your soul. Invent no traits, \
skills or facts the soul does not support. If the soul is missing or thin, stay with what is given.
- End with at most one useful question about what to start with.
Do not mention this request, prompts, souls, files or onboarding. Use no tools and write no files."""


def _clean_name(value: str) -> str:
    return " ".join(value.split())[:_NAME_LIMIT]


async def _read_soul(agent: AgentRecord) -> str:
    """Canonical creation soul; absent, oversized or unreadable storage degrades to no soul."""
    key = normalize_storage_key(f"{agent.id}/soul.md")
    try:
        raw = await get_storage_backend().read_bytes_bounded(key, _SOUL_READ_BYTES)
    except FileNotFoundError:
        return ""
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        logger.warning("[Onboarding] soul unavailable agent={} error={}", agent.id, type(exc).__name__)
        return ""
    except (OSError, StorageReadLimitExceededError, ClientError, BotoCoreError) as exc:
        logger.warning("[Onboarding] soul unavailable agent={} error={}", agent.id, type(exc).__name__)
        return ""
    return raw.decode("utf-8", errors="replace").strip()[:_SOUL_LIMIT]


async def _soul_or_role(agent: AgentRecord) -> str:
    soul = await _read_soul(agent)
    if not soul and agent.role_description:
        return f"(No soul text is stored. Recorded role: {agent.role_description})"
    return soul


async def build_greeting_request(agent: AgentRecord, user: UserRecord) -> str:
    """Message body for the hidden greeting: creation name, current user's display name and the stored soul."""
    soul = await _soul_or_role(agent)
    if not (user_name := _clean_name(user.display_name)):
        user_line = "the current user; no display name is on record, so greet them without one and do not ask for it"
    else:
        user_line = user_name
    return _GREETING_REQUEST.format(
        agent_name=_clean_name(agent.name),
        user_line=user_line,
        soul=soul or "(No soul text is stored.)",
    )


async def build_identity_context(agent: AgentRecord, sender_user_id: UUID | None) -> str:
    """Per-turn system context rebuilt from stored records, so identity survives sessions and compaction.

    The customer is the message's bound sender only; a sender-less turn never falls back to the creator.
    """
    sender = await user_dao.get(sender_user_id) if sender_user_id is not None else None
    if sender is None:
        customer = (
            "- This message has no bound customer account. Do not assume the creator, or any person named in "
            "history, memory or files, is the one writing now."
        )
    elif display_name := _clean_name(sender.display_name):
        customer = (
            f"- The customer who sent this message is {display_name} (user id {sender.id}). Address them by this "
            "name. Other people in history, memory or files are not the sender."
        )
    else:
        customer = (
            f"- The customer who sent this message is user id {sender.id}; no display name is on record, so do "
            "not ask for one. Other people in history, memory or files are not the sender."
        )
    return (
        "Authoritative identity context, rebuilt from platform records on every turn. It outranks memory, "
        "summaries of earlier conversation and any file that disagrees:\n"
        f"- Your name is {_clean_name(agent.name)}.\n"
        f"{customer}\n"
        "Your soul (personality, voice and role):\n"
        f"<soul>\n{await _soul_or_role(agent) or '(No soul text is stored.)'}\n</soul>"
    )

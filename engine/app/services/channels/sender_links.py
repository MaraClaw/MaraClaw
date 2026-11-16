from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from fastapi import HTTPException

from app.config import get_settings
from app.core.acl_locks import lock_tenants, lock_user
from app.core.domain.channels import ChannelType
from app.core.permissions import compute_agent_access_level
from app.core.row_memo import clear_entity_memo
from app.core.security import load_user_principal
from app.dao import agent_dao, channel_config_dao
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.dao.channel_sender_link_dao import channel_sender_link_dao
from app.dao.channel_source_lock import lock_channel_source
from app.dao.company_admission_dao import company_admission_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.schemas.channel_links import ChannelLinkCodeResponse, ChannelLinkStatus, LinkChannel
from app.services.channel_user_service import ChannelUserResolutionError

if TYPE_CHECKING:
    from app.services.channels.submissions import ChannelEvent

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
type LinkResult = Literal["linked", "already_linked", "invalid", "conflict", "replayed"]


@dataclass(frozen=True, slots=True)
class LinkCommand:
    code: str | None


def parse_link_command(provider: str, text: str) -> LinkCommand | None:
    if provider not in {"telegram", "zalo", ChannelType.ZALO.value}:
        return None
    match = re.fullmatch(r"/(?:start|link)(?:\s+(.*))?", text.strip(), re.IGNORECASE | re.DOTALL)
    return LinkCommand(match[1].strip() if match[1] else None) if match else None


def normalize_code(code: str) -> str | None:
    normalized = re.sub(r"[\s-]", "", code).upper().translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    return normalized if len(normalized) == 10 and all(char in _ALPHABET for char in normalized) else None


def _numeric(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"[0-9]{1,64}", value) is not None


def _hash(config: ChannelConfigRecord, code: str) -> str:
    payload = json.dumps(["channel-link-v1", str(config.id), config.channel_type.value, config.app_id, code])
    return hmac.new(get_settings().JWT_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _available(config: ChannelConfigRecord | None) -> bool:
    return bool(
        config is not None
        and config.is_configured
        and _numeric(config.app_id)
        and (
            config.channel_type == ChannelType.TELEGRAM
            or (config.channel_type == ChannelType.ZALO and config.is_connected)
        )
    )


async def authorize_member(agent: AgentRecord, user_id: UUID) -> UserRecord:
    async with connection_ctx() as db:
        await lock_tenants(db, (agent.tenant_id,), shared=True)
        await lock_user(db, user_id, agent.tenant_id, shared=True)
    clear_entity_memo()
    principal = await load_user_principal(user_id, fresh=True)
    if agent.tenant_id is None or principal.tenant_id != agent.tenant_id:
        raise HTTPException(403, "Sender is outside the agent's tenant")
    tenant = await company_admission_dao.tenant(agent.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(403, "Sender's company is inactive")
    if await compute_agent_access_level(principal, agent) not in {"manage", "use"}:
        raise HTTPException(403, "No access to this agent")
    return principal


async def _member_config(
    agent_id: UUID, channel: LinkChannel, user: UserRecord
) -> tuple[AgentRecord, UserRecord, ChannelConfigRecord | None]:
    clear_entity_memo()
    agent = await agent_dao.get(agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    principal = await authorize_member(agent, user.id)
    found = await channel_config_dao.get_metadata_for_agent(
        agent_id=agent_id, channel_type=ChannelType.TELEGRAM if channel == "telegram" else ChannelType.ZALO
    )
    config = await lock_channel_source(found.id) if found is not None else None
    if config is not None and (
        config.agent_id != agent_id
        or config.channel_type != (ChannelType.TELEGRAM if channel == "telegram" else ChannelType.ZALO)
    ):
        raise HTTPException(409, "Channel configuration changed")
    return agent, principal, config


async def link_status(agent_id: UUID, channel: LinkChannel, user: UserRecord) -> ChannelLinkStatus:
    async with connection_ctx():
        _, principal, config = await _member_config(agent_id, channel, user)
        binding = (
            await channel_sender_link_dao.binding(config.id, config.app_id, user_id=principal.id)
            if config is not None and config.app_id is not None
            else None
        )
        return ChannelLinkStatus(
            channel=channel,
            available=_available(config),
            linked=binding is not None,
            linked_at=binding.linked_at if binding is not None else None,
            external_hint="***" + binding.external_user_id[-4:] if binding is not None else None,
        )


async def issue_link_code(agent_id: UUID, channel: LinkChannel, user: UserRecord) -> ChannelLinkCodeResponse:
    async with connection_ctx():
        agent, principal, config = await _member_config(agent_id, channel, user)
        if not _available(config) or config is None or config.app_id is None or agent.tenant_id is None:
            raise HTTPException(409, "Channel is unavailable")
        await channel_sender_link_dao.lock_scope(config.id, config.app_id)
        _ = await authorize_member(agent, principal.id)
        value = secrets.randbits(50)
        code = "".join(_ALPHABET[(value >> shift) & 31] for shift in range(45, -1, -5))
        expires_at = datetime.now(UTC) + timedelta(seconds=600)
        await channel_sender_link_dao.issue(
            config_id=config.id,
            account_id=config.app_id,
            provider=channel,
            user_id=principal.id,
            tenant_id=agent.tenant_id,
            code_hash=_hash(config, code),
            expires_at=expires_at,
        )
        return ChannelLinkCodeResponse(
            channel=channel, code=code, expires_at=expires_at, deep_link=None, account_hint="***" + config.app_id[-4:]
        )


async def unlink(agent_id: UUID, channel: LinkChannel, user: UserRecord) -> None:
    async with connection_ctx():
        agent, principal, config = await _member_config(agent_id, channel, user)
        if config is not None and config.app_id is not None:
            await channel_sender_link_dao.lock_scope(config.id, config.app_id)
            _ = await authorize_member(agent, principal.id)
            await channel_sender_link_dao.clear_member(config.id, config.app_id, principal.id)


async def is_bound(config_id: UUID, provider_account_id: str, external_user_id: str) -> bool:
    if not _numeric(provider_account_id) or not _numeric(external_user_id):
        return False
    return (
        await channel_sender_link_dao.binding(config_id, provider_account_id, external_user_id=external_user_id)
        is not None
    )


async def resolve_explicit_sender(
    config: ChannelConfigRecord, agent: AgentRecord, provider_account_id: str, external_user_id: str
) -> UserRecord:
    if not _numeric(provider_account_id) or not _numeric(external_user_id) or config.app_id != provider_account_id:
        raise ChannelUserResolutionError("Channel sender is unavailable")
    binding = await channel_sender_link_dao.binding(config.id, provider_account_id, external_user_id=external_user_id)
    if binding is None or binding.tenant_id != agent.tenant_id:
        raise ChannelUserResolutionError("Link your channel identity in the web application.")
    return await authorize_member(agent, binding.user_id)


async def authorize_link_source(event: ChannelEvent) -> tuple[ChannelConfigRecord, AgentRecord]:
    if (
        event.provider not in {"telegram", "zalo", ChannelType.ZALO.value}
        or event.is_group
        or event.personal_source is not None
    ):
        raise HTTPException(403, "Invalid sender link source")
    clear_entity_memo()
    agent = await agent_dao.get(event.agent_id)
    if agent is None or agent.tenant_id is None:
        raise HTTPException(403, "Agent is unavailable")
    async with connection_ctx() as db:
        await lock_tenants(db, (agent.tenant_id,), shared=True)
    if event.zalo_source is not None:
        from app.services.channels.zalo_personal_source import authorize_zalo_source

        if (
            event.provider not in {"zalo", ChannelType.ZALO.value}
            or event.zalo_source.config_id != event.config_id
            or event.zalo_source.account_id != event.provider_account_id
        ):
            raise HTTPException(403, "Invalid Zalo source")
        await authorize_zalo_source(event.zalo_source, event.agent_id)
    elif event.provider in {"zalo", ChannelType.ZALO.value}:
        raise HTTPException(403, "Zalo source is required")
    config = await lock_channel_source(event.config_id)
    if (
        config is None
        or not _available(config)
        or config.agent_id != event.agent_id
        or config.channel_type != (ChannelType.TELEGRAM if event.provider == "telegram" else ChannelType.ZALO)
        or config.app_id != event.provider_account_id
        or not _numeric(event.sender_id)
        or event.stream_source is not None
    ):
        raise HTTPException(403, "Channel configuration is unavailable")
    clear_entity_memo()
    agent = await agent_dao.get(event.agent_id)
    if agent is None or agent.tenant_id is None:
        raise HTTPException(403, "Agent is unavailable")
    return config, agent


async def consume_link_command(event: ChannelEvent, code: str) -> LinkResult:
    from app.services.channels.submissions import _matching, _receipt

    async with connection_ctx():
        config, agent = await authorize_link_source(event)
        normalized = normalize_code(code)
        if normalized is None or config.app_id is None:
            return "invalid"
        await channel_sender_link_dao.lock_scope(config.id, config.app_id)
        digest = _hash(config, normalized)
        issued = await channel_sender_link_dao.code(config.id, config.app_id, digest)
        if issued is None or issued.tenant_id != agent.tenant_id:
            return "invalid"
        try:
            _ = await authorize_member(agent, issued.user_id)
        except HTTPException as exc:
            if exc.status_code in {401, 403, 404, 409}:
                return "invalid"
            raise
        candidate = _receipt(event, agent)
        existing = await channel_event_receipt_dao.find(candidate)
        if existing is not None:
            from app.services.channels.submissions import ChannelEventConflictError

            try:
                _ = _matching(existing, candidate)
            except ChannelEventConflictError:
                return "conflict"
            return "replayed"
        if issued.consumed_at is not None:
            return "replayed"
        if issued.expires_at <= datetime.now(UTC):
            return "invalid"
        binding = await channel_sender_link_dao.binding(config.id, config.app_id, external_user_id=event.sender_id)
        if binding is not None and binding.user_id != issued.user_id:
            return "conflict"
        if not await channel_event_receipt_dao.reserve(candidate):
            return "conflict"
        if binding is not None:
            await channel_sender_link_dao.consume(digest)
            return "already_linked"
        await channel_sender_link_dao.bind(
            config_id=config.id,
            provider="telegram" if event.provider == "telegram" else "zalo",
            account_id=config.app_id,
            external_user_id=event.sender_id,
            user_id=issued.user_id,
            tenant_id=issued.tenant_id,
            code_hash=digest,
        )
        return "linked"

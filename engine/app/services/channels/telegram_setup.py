"""Telegram webhook registration with short, encrypted configuration writes."""

import secrets
from ipaddress import ip_address
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from app.core.domain.channels import ChannelType
from app.core.stored_credentials import PreservedCredential, StoredCredentialError, open_credential
from app.dao.channel_config_dao import channel_config_dao
from app.dao.channel_sender_link_dao import channel_sender_link_dao
from app.records.channel_config import ChannelConfigRecord
from app.schemas.telegram import (
    TelegramChannelConfigUpdate,
    TelegramCredentials,
    TelegramSecretToken,
    TelegramSetWebhookResponse,
)
from app.services.channels.outbound_http import outbound_client
from app.services.channels.telegram_outbound import _TelegramBotURL
from app.services.platform_service import PublicBaseUrlNotConfiguredError, platform_service

_SECRET_TOKEN = TypeAdapter(TelegramSecretToken)


def _open_secret(value: str | None) -> str | None:
    if isinstance(value, PreservedCredential):
        return open_credential(value.stored, value.context)
    return value


async def telegram_webhook_url(config_id: UUID) -> str:
    try:
        public_base = await platform_service.get_public_base_url()
        url = httpx.URL(public_base)
    except PublicBaseUrlNotConfiguredError, httpx.InvalidURL:
        raise HTTPException(503, "Telegram requires a public HTTPS engine URL") from None
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.port not in (None, 443, 80, 88, 8443)
        or url.host == "localhost"
        or url.host.endswith(".localhost")
    ):
        raise HTTPException(503, "Telegram requires a public HTTPS engine URL")
    try:
        address = ip_address(url.host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise HTTPException(503, "Telegram requires a public HTTPS engine URL")
    return f"{public_base.rstrip('/')}/api/channel/telegram/{config_id}/webhook"


async def _register_webhook(credentials: TelegramCredentials, webhook_url: str) -> None:
    async def redact_trace(name: str, info: dict[str, object]) -> None:
        if name.endswith((".complete", ".failed")):
            for key, value in info.items():
                rendered = repr(value)
                for secret in (credentials.bot_token, credentials.secret_token, webhook_url):
                    rendered = rendered.replace(secret, "[redacted]")
                if rendered != repr(value):
                    info[key] = rendered

    client = outbound_client()
    request = client.build_request(
        "POST",
        "https://api.telegram.org/bot[redacted]/setWebhook",
        json={"url": webhook_url, "secret_token": credentials.secret_token, "allowed_updates": ["message"]},
        extensions={"trace": redact_trace},
    )
    # HTTPX drops subclass display methods in build_request; assign the safe URL afterwards.
    request.url = _TelegramBotURL(f"https://api.telegram.org/bot{credentials.bot_token}/setWebhook")
    try:
        response = await client.send(request, follow_redirects=False)
    except httpx.RequestError:
        raise HTTPException(502, "Telegram webhook registration is unavailable") from None
    if not response.is_success:
        raise HTTPException(502, "Telegram rejected webhook registration")
    try:
        body = TelegramSetWebhookResponse.model_validate_json(response.content)
    except ValidationError:
        raise HTTPException(502, "Invalid Telegram webhook registration response") from None
    if not body.ok or body.result is not True:
        raise HTTPException(502, "Telegram rejected webhook registration")


async def configure_telegram(agent_id: UUID, data: TelegramChannelConfigUpdate) -> ChannelConfigRecord:
    # Metadata lets each credential decrypt independently when repairing a damaged bot token.
    existing = await channel_config_dao.get_metadata_for_agent(agent_id=agent_id, channel_type=ChannelType.TELEGRAM)
    try:
        bot_token = (
            data.bot_token if data.bot_token is not None else _open_secret(existing.app_secret) if existing else None
        )
    except StoredCredentialError:
        raise HTTPException(422, "Replace the Telegram bot_token") from None
    if existing is None:
        secret_token = secrets.token_urlsafe(32)
    else:
        try:
            secret_token = _SECRET_TOKEN.validate_python(_open_secret(existing.verification_token))
        except StoredCredentialError, ValidationError:
            if data.bot_token is None:
                raise HTTPException(422, "Replace the Telegram bot_token to repair its credentials") from None
            secret_token = secrets.token_urlsafe(32)
    try:
        credentials = TelegramCredentials.model_validate({"bot_token": bot_token, "secret_token": secret_token})
    except ValidationError:
        raise HTTPException(422, "A valid Telegram bot_token is required") from None
    config_id = existing.id if existing is not None else uuid4()
    webhook_url = await telegram_webhook_url(config_id)
    # No DB checkout is held during Telegram I/O; failures leave the stored configuration untouched.
    await _register_webhook(credentials, webhook_url)
    account_id = credentials.bot_token.partition(":")[0]
    values: dict[str, object] = {
        "app_id": account_id,
        "app_secret": credentials.bot_token,
        "verification_token": credentials.secret_token,
        "encrypt_key": None,
        "extra_config": {},
        "is_configured": True,
    }
    async with channel_config_dao.session():
        if existing is None:
            config = await channel_config_dao.create(
                obj_in={"id": config_id, "agent_id": agent_id, "channel_type": ChannelType.TELEGRAM, **values}
            )
        else:
            config = await channel_config_dao.update(db_obj=existing, obj_in=values)
        await channel_sender_link_dao.invalidate_other_accounts(config.id, account_id)
    return config

import re
from datetime import UTC, datetime, timedelta
from typing import override

import httpx
from pydantic import TypeAdapter, ValidationError

from app.records.channel_config import ChannelConfigRecord
from app.records.outbound_plan import OutboundProvider
from app.schemas.telegram import TelegramBotToken, TelegramSendResponse
from app.services.channels.outbound_errors import ProviderDeliveryError, ProviderPayloadRejectedError, provider_deadline
from app.services.channels.outbound_http import outbound_client

_BOT_TOKEN = TypeAdapter(TelegramBotToken)


class _TelegramBotURL(httpx.URL):
    """Keep the credential on the wire while HTTPX logs only its safe representation."""

    @override
    def __str__(self) -> str:
        return "https://api.telegram.org/bot[redacted]/sendMessage"

    @override
    def __repr__(self) -> str:
        return f"URL('{self}')"


async def send_telegram_text(config: ChannelConfigRecord, destination: str, text: str) -> str:
    """Send one 1-4096 character plain-text chunk; return the native message id."""
    provider = OutboundProvider("telegram")
    try:
        token = _BOT_TOKEN.validate_python(config.app_secret)
    except ValidationError:
        raise ProviderDeliveryError(provider, "credentials_unavailable") from None
    if re.fullmatch(r"[1-9][0-9]*", destination) is None:
        raise ProviderPayloadRejectedError(provider, "invalid_destination")
    if not 1 <= len(text) <= 4096:
        raise ProviderPayloadRejectedError(provider, "invalid_content")

    async def redact_trace(name: str, info: dict[str, object]) -> None:
        # HTTPcore invokes this before logging completion metadata; its operation result stays untouched.
        if name.endswith((".complete", ".failed")):
            for key, value in info.items():
                rendered = repr(value)
                if token in rendered:
                    info[key] = rendered.replace(token, "[redacted]")

    client = outbound_client()
    request = client.build_request(
        "POST",
        "https://api.telegram.org/bot[redacted]/sendMessage",
        json={"chat_id": destination, "text": text},
        extensions={"trace": redact_trace},
    )
    # Assign after build_request: HTTPX's URL constructor drops subclass display methods.
    request.url = _TelegramBotURL(f"https://api.telegram.org/bot{token}/sendMessage")
    try:
        response = await client.send(request, follow_redirects=False)
    except httpx.RequestError:
        raise ProviderDeliveryError(provider, "transport_failed") from None
    now = datetime.now(UTC)
    deadline = provider_deadline(response, now)
    try:
        body = TelegramSendResponse.model_validate_json(response.content)
    except ValidationError:
        code = f"http_{response.status_code}" if not response.is_success else "invalid_acknowledgment"
        raise ProviderDeliveryError(provider, code, deadline) from None
    if body.parameters is not None and body.parameters.retry_after is not None:
        delay = now + timedelta(seconds=body.parameters.retry_after)
        deadline = max(deadline, delay) if deadline is not None else delay
    if not response.is_success:
        raise ProviderDeliveryError(provider, f"http_{response.status_code}", deadline)
    if not body.ok:
        code = f"api_{body.error_code}" if body.error_code is not None else "api_rejected"
        raise ProviderDeliveryError(provider, code, deadline)
    if body.result is None:
        raise ProviderDeliveryError(provider, "missing_acknowledgment")
    return str(body.result.message_id)

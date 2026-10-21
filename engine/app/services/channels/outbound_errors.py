from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import override

import httpx

from app.core.json_types import JsonObject, json_object_from, json_object_from_response
from app.records.outbound_plan import OutboundProvider


@dataclass
class ProviderDeliveryError(RuntimeError):
    provider: OutboundProvider
    code: str
    retry_at: datetime | None = None

    @override
    def __str__(self) -> str:
        return f"{self.provider}: {self.code}"


class ProviderPayloadRejectedError(ProviderDeliveryError):
    pass


def provider_deadline(response: httpx.Response, now: datetime) -> datetime | None:
    deadlines: list[datetime] = []
    header = response.headers["Retry-After"].strip() if "Retry-After" in response.headers else ""
    if header:
        try:
            seconds = float(header)
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
            except ValueError, TypeError, OverflowError:
                pass
            else:
                deadlines.append(date.replace(tzinfo=UTC) if date.tzinfo is None else date)
        else:
            if isfinite(seconds) and 0 <= seconds <= timedelta.max.total_seconds():
                with suppress(OverflowError):
                    deadlines.append(now + timedelta(seconds=seconds))
    if response.request.url.host == "discord.com" and response.status_code == 429:
        try:
            body = json_object_from_response(response)
        except ValueError:
            body = {}
        delay = body.get("retry_after")
        if isinstance(delay, (int, float)) and not isinstance(delay, bool):
            with suppress(OverflowError):
                if isfinite(delay) and delay >= 0:
                    deadlines.append(now + timedelta(seconds=delay))
    return max(deadlines) if deadlines else None


def response_body(response: httpx.Response, provider: OutboundProvider) -> JsonObject:
    if response.status_code >= 400:
        if provider == "discord" and response.status_code == 400:
            body = json_object_from_response(response)
            if body.get("code") == 50035 and bool(json_object_from(body.get("errors")).get("content")):
                raise ProviderPayloadRejectedError(provider, "invalid_content")
        response.raise_for_status()
    body = json_object_from_response(response)
    if provider == "slack" and body.get("error") in {"msg_too_long", "no_text"}:
        raise ProviderPayloadRejectedError(provider, str(body["error"]))
    return body


def retry_at(error: Exception, now: datetime) -> datetime | None:
    if isinstance(error, ProviderDeliveryError):
        return error.retry_at
    if isinstance(error, httpx.HTTPStatusError):
        return provider_deadline(error.response, now)
    return None

"""Throttles of the public auth routes, applied before any password check, lookup or email send.

An account is keyed by a digest of the submitted identifier whether or not it exists, and every refusal is the shared
429, so neither status, body, headers nor skipped work say which accounts are real. The client address comes from
``client_address.get_client_ip``, so users behind one trusted proxy keep separate budgets.
"""

import hashlib
from typing import Final, Literal
from uuid import UUID

from fastapi import Request

from app.config import get_settings
from app.core.client_address import get_client_ip
from app.core.rate_limit import Limit, enforce

_UNKNOWN_ADDRESS: Final = "unknown"

type AccountRoute = Literal["login", "forgot-password", "resend-verification"]


async def _address_limit(request: Request, route: str) -> Limit:
    settings = get_settings()
    return Limit(
        f"auth:{route}:address",
        await get_client_ip(request) or _UNKNOWN_ADDRESS,
        settings.AUTH_RATE_LIMIT_ADDRESS_ATTEMPTS,
        settings.AUTH_RATE_LIMIT_WINDOW_SECONDS,
    )


async def throttle_account(route: AccountRoute, identifier: str, request: Request) -> None:
    """Budget for one account identifier and one client address on a route that takes an email."""
    settings = get_settings()
    account = hashlib.sha256(identifier.strip().lower().encode()).hexdigest()[:32]
    await enforce(
        await _address_limit(request, route),
        Limit(
            f"auth:{route}:account",
            account,
            settings.AUTH_RATE_LIMIT_ACCOUNT_ATTEMPTS,
            settings.AUTH_RATE_LIMIT_WINDOW_SECONDS,
        ),
    )


async def throttle_sign_in(request: Request) -> None:
    """Address budget of the sign-in routes that carry no account identifier; shares the login address bucket."""
    await enforce(await _address_limit(request, "login"))


async def throttle_ticket_address(request: Request) -> None:
    """Address budget of the chat and download ticket routes; runs before the caller is authenticated."""
    await enforce(await _address_limit(request, "ticket"))


async def throttle_ticket_user(user_id: str | UUID) -> None:
    """Budget for one signed-in user; runs before any access check, lookup or ticket write."""
    settings = get_settings()
    await enforce(
        Limit(
            "auth:ticket:user",
            str(user_id),
            settings.TICKET_RATE_LIMIT_USER_ATTEMPTS,
            settings.AUTH_RATE_LIMIT_WINDOW_SECONDS,
        )
    )

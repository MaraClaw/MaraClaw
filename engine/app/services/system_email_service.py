"""System email delivery; identity mail is platform-only, organization mail is scoped."""

from __future__ import annotations

import asyncio
import smtplib
from contextvars import Context
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from functools import partial
from typing import Literal, TypedDict
from uuid import UUID

import anyio

from app.core.email import send_smtp_email
from app.core.json_types import json_as_str, json_object_from
from app.core.logging import get_logger
from app.core.outbound_policy import OutboundPolicyError
from app.services.email_config import (
    SystemEmailConfig,
    resolve_email_config_async,
)

__all__ = [
    "DEFAULT_EMAIL_TEMPLATES",
    "EMAIL_TEMPLATE_VARIABLES",
    "EmailAttemptOutcome",
    "EmailTemplate",
    "EmailTemplates",
    "SystemEmailConfig",
    "attempt_email",
    "get_email_templates",
    "render_email_template",
    "resolve_email_config_async",
    "send_company_invitation_email",
    "send_password_reset_email",
    "send_system_email",
]

logger = get_logger(__name__)


class EmailTemplate(TypedDict):
    subject: str
    body: str


type EmailTemplates = dict[str, EmailTemplate]


@dataclass(frozen=True, slots=True)
class EmailAttemptOutcome:
    """Broadcast SMTP result; error codes never include message content or credentials."""

    kind: Literal["sent", "not_configured", "permanent", "transient"]
    error: str | None = None


async def attempt_email(
    to: str, subject: str, body: str, *, message_id: str, tenant_id: UUID | None = None
) -> EmailAttemptOutcome:
    """Attempt one broadcast message without inheriting a caller's DB transaction.

    Cancellation propagates, even while the native SMTP thread is still running.
    The worker must then exit without settling or starting another batch.
    """
    resolution = resolve_email_config_async() if tenant_id is None else resolve_email_config_async(tenant_id)
    config = await asyncio.create_task(resolution, context=Context())
    if config is None:
        return EmailAttemptOutcome("not_configured", "not_configured")
    send = partial(
        _send_email_with_config_sync,
        replace(config, smtp_timeout_seconds=min(config.smtp_timeout_seconds, 15)),
        to,
        subject,
        body,
        message_id=message_id,
    )
    try:
        await anyio.to_thread.run_sync(partial(Context().run, send), abandon_on_cancel=True)
    except smtplib.SMTPRecipientsRefused as exc:
        codes = [code for code, _message in exc.recipients.values()]
        permanent = bool(codes) and all(500 <= code < 600 for code in codes)
        return EmailAttemptOutcome("permanent" if permanent else "transient", "recipient_refused")
    except smtplib.SMTPResponseException as exc:
        kind = "permanent" if 500 <= exc.smtp_code < 600 else "transient"
        return EmailAttemptOutcome(kind, f"smtp_{exc.smtp_code}")
    except (smtplib.SMTPNotSupportedError, ValueError, UnicodeError, OutboundPolicyError) as exc:
        return EmailAttemptOutcome("permanent", type(exc).__name__)
    except (smtplib.SMTPException, OSError) as exc:
        return EmailAttemptOutcome("transient", type(exc).__name__)
    return EmailAttemptOutcome("sent")


async def send_system_email(to: str, subject: str, body: str, *, tenant_id: UUID | None = None) -> None:
    """Send a plain-text system email without blocking the event loop.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body text
    """
    config = await resolve_email_config_async() if tenant_id is None else await resolve_email_config_async(tenant_id)

    if not config:
        logger.warning("System email not configured, skipped sending to {recipient}", recipient=to)
        return

    await asyncio.to_thread(_send_email_with_config_sync, config, to, subject, body)


def _send_email_with_config_sync(
    config: SystemEmailConfig, to: str, subject: str, body: str, *, message_id: str | None = None
) -> None:
    """Send email with provided config."""
    msg = MIMEMultipart()
    msg["From"] = formataddr((config.from_name, config.from_address))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid() if message_id is None else message_id
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.attach(MIMEText(body, "plain", "utf-8"))

    send_smtp_email(
        host=config.smtp_host,
        port=config.smtp_port,
        user=config.smtp_username,
        password=config.smtp_password,
        from_addr=config.from_address,
        to_addrs=[to],
        msg_string=msg.as_string(),
        use_ssl=config.smtp_ssl,
        timeout=config.smtp_timeout_seconds,
        public_only=config.public_only,
    )


async def send_password_reset_email(
    to: str,
    display_name: str,
    reset_url: str,
    expiry_minutes: int,
) -> None:
    """Send a password reset email using the configured template.

    Args:
        to: Recipient email
        display_name: User display name
        reset_url: Password reset URL
        expiry_minutes: Token expiry time in minutes
    """
    variables = {
        "display_name": display_name,
        "reset_url": reset_url,
        "expiry_minutes": str(expiry_minutes),
    }
    subject, body = await render_email_template("password_reset", variables)
    await send_system_email(to, subject, body)


async def send_company_invitation_email(
    to: str,
    inviter_name: str,
    company_name: str,
    invite_url: str,
    *,
    tenant_id: UUID,
    invite_code: str,
    expires_at: datetime,
) -> None:
    """Send a company invitation email using the configured template.

    Args:
        to: Recipient email
        inviter_name: Name of the person inviting
        company_name: Name of the company
        invite_url: Registration URL with invitation code
        invite_code: The single-use code, also embedded in the URL
        expires_at: When the code stops working
    """
    expires_text = expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    variables = {
        "inviter_name": inviter_name,
        "company_name": company_name,
        "invite_url": invite_url,
        "invite_code": invite_code,
        "expires_at": expires_text,
    }
    subject, body = await render_email_template("company_invitation", variables)
    if expires_text not in body:
        body += f"\n\nThis invitation expires on {expires_text}."
    await send_system_email(to, subject, body, tenant_id=tenant_id)


# ── Email Templates ──────────────────────────────────────────────────────────

# Default templates for each email scenario.
# Each scenario has a fixed set of available variables (using {{variable}} syntax).
DEFAULT_EMAIL_TEMPLATES: EmailTemplates = {
    "email_verification": {
        "subject": "Verify your MaraClaw email address",
        "body": (
            "Hello {{display_name}},\n\n"
            "Welcome to MaraClaw! Please use the following 6-digit code to verify your email address:\n\n"
            "Verification code: {{verification_code}}\n\n"
            "Or open this link: {{verification_url}}\n\n"
            "This code expires in {{expiry_minutes}} minutes. "
            "If you did not create an account, you can ignore this email."
        ),
    },
    "password_reset": {
        "subject": "Reset your MaraClaw password",
        "body": (
            "Hello {{display_name}},\n\n"
            "We received a request to reset your MaraClaw password.\n\n"
            "Reset link: {{reset_url}}\n\n"
            "This link expires in {{expiry_minutes}} minutes. "
            "If you did not request this, you can ignore this email."
        ),
    },
    "company_invitation": {
        "subject": "{{inviter_name}} invited you to join {{company_name}} on MaraClaw",
        "body": (
            "Hello,\n\n"
            "{{inviter_name}} has invited you to join their team '{{company_name}}' on MaraClaw.\n\n"
            "To accept the invitation and create your account, please click the link below:\n\n"
            "{{invite_url}}\n\n"
            "Invitation code: {{invite_code}}\n"
            "It can only be used by this email address and expires on {{expires_at}}.\n\n"
            "If you don't want to join this team or didn't expect this invitation, you can ignore this email."
        ),
    },
}

# Fixed available variables per scenario (for frontend display)
EMAIL_TEMPLATE_VARIABLES: dict[str, list[str]] = {
    "email_verification": ["display_name", "verification_code", "verification_url", "expiry_minutes"],
    "password_reset": ["display_name", "reset_url", "expiry_minutes"],
    "company_invitation": ["inviter_name", "company_name", "invite_url", "invite_code", "expires_at"],
}


async def get_email_templates() -> EmailTemplates:
    """Load email templates from DB, falling back to defaults.

    Returns:
        A dict mapping scenario_key -> {"subject": str, "body": str}
    """
    templates = dict(DEFAULT_EMAIL_TEMPLATES)  # start with defaults
    return await _load_templates_from_db(templates)


async def _load_templates_from_db(templates: EmailTemplates) -> EmailTemplates:
    """Internal helper: overlay DB-saved templates on top of defaults."""
    from app.dao import system_setting_dao

    try:
        saved_raw: object = await system_setting_dao.get_value("email_templates", {})
        saved = json_object_from(saved_raw)
        if saved:
            for key in templates:
                entry = json_object_from(saved.get(key))
                if not entry:
                    continue
                subject = json_as_str(entry.get("subject"))
                if subject not in (None, ""):
                    templates[key]["subject"] = subject
                body = json_as_str(entry.get("body"))
                if body not in (None, ""):
                    templates[key]["body"] = body
    except Exception as e:
        logger.warning("Error loading email templates from DB: {error}", error=e)

    return templates


def _render_template(template_str: str, variables: dict[str, str]) -> str:
    """Replace {{variable_name}} placeholders with actual values."""
    result = template_str
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


async def render_email_template(
    scenario_key: str,
    variables: dict[str, str],
) -> tuple[str, str]:
    """Render an email template for a given scenario.

    Args:
        scenario_key: One of the known scenario keys (e.g. 'email_verification')
        variables: Dict of variable_name -> value to substitute

    Returns:
        (subject, body) tuple with variables substituted
    """
    templates = await get_email_templates()
    empty: EmailTemplate = {"subject": "", "body": ""}
    template: EmailTemplate = templates.get(scenario_key) or DEFAULT_EMAIL_TEMPLATES.get(scenario_key) or empty

    subject = _render_template(template.get("subject", ""), variables)
    body = _render_template(template.get("body", ""), variables)
    return subject, body

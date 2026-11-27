"""Email service - IMAP/SMTP email operations for agent tools.

Supports all major email providers via preset configurations.
Each agent stores its own email credentials in per-agent tool config.
"""

import base64
import email as email_lib
import imaplib
import os
import secrets
import smtplib
import ssl
import tempfile
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.header import decode_header
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.policy import compat32
from email.utils import make_msgid, parseaddr
from functools import partial
from pathlib import Path
from typing import IO, BinaryIO, NotRequired, TypedDict, cast

from app.config import get_settings
from app.core.email import force_ipv4, send_smtp_email, send_smtp_stream
from app.core.json_types import json_as_bool, json_as_str_or
from app.core.logging import logger
from app.core.thread_lanes import ThreadLane
from app.services.storage_runtime.base import (
    StorageBackend,
    StorageReadChangedError,
    StorageReadLimitExceededError,
)

AGENT_EMAIL_LANE = ThreadLane(get_settings().AGENT_EMAIL_LANE_THREADS, name="agent-email-lane")
# Seconds one socket operation of a mail exchange may take, IMAP or SMTP. A send, a read and a reply use the first (the
# timeout the SMTP side of send_email and reply_email always had), the connection test the second (its SMTP timeout).
_EXCHANGE_TIMEOUT_SECONDS = 15
_PROBE_TIMEOUT_SECONDS = 10


class EmailProviderPreset(TypedDict):
    label: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool
    help_url: str
    help_text: str


class EmailConfig(TypedDict):
    email_provider: NotRequired[str]
    email_address: NotRequired[str]
    auth_code: NotRequired[str]
    imap_host: NotRequired[str]
    imap_port: NotRequired[int | str]
    smtp_host: NotRequired[str]
    smtp_port: NotRequired[int | str]
    smtp_ssl: NotRequired[bool]


class ResolvedEmailConfig(TypedDict):
    email_address: str
    auth_code: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool


class EmailConnectionResult(TypedDict):
    ok: bool
    imap: NotRequired[str]
    smtp: NotRequired[str]
    error: NotRequired[str]


# Preset email provider configurations
EMAIL_PROVIDERS: dict[str, EmailProviderPreset] = {
    "qq": {
        "label": "QQ Mail",
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "https://service.mail.qq.com/detail/0/310",
        "help_text": "Settings → Account → POP3/IMAP/SMTP → Enable IMAP → Generate authorization code",
    },
    "163": {
        "label": "163 Mail",
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2",
        "help_text": "Settings → POP3/SMTP/IMAP → Enable IMAP → Set authorization code",
    },
    "gmail": {
        "label": "Gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "https://support.google.com/accounts/answer/185833",
        "help_text": "Google Account → Security → App passwords → Generate app password",
    },
    "outlook": {
        "label": "Outlook / Microsoft 365",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_ssl": False,  # Uses STARTTLS
        "help_url": "https://support.microsoft.com/en-us/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-4bf7-4851-ad95-6d07799387e9",
        "help_text": "Microsoft Account → Security → App passwords",
    },
    "qq_enterprise": {
        "label": "Tencent Enterprise Mail",
        "imap_host": "imap.exmail.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.exmail.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "https://open.work.weixin.qq.com/help2/pc/18624",
        "help_text": "Enterprise Mail → Settings → Client-specific password → Generate new password",
    },
    "aliyun": {
        "label": "Alibaba Enterprise Mail",
        "imap_host": "imap.qiye.aliyun.com",
        "imap_port": 993,
        "smtp_host": "smtp.qiye.aliyun.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "",
        "help_text": "Use your email password directly",
    },
}


def _as_port(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def resolve_config(config: EmailConfig) -> ResolvedEmailConfig:
    """Resolve a user config into full IMAP/SMTP settings using provider presets."""
    provider = json_as_str_or(config.get("email_provider"), "custom") or "custom"
    result: ResolvedEmailConfig = {
        "email_address": json_as_str_or(config.get("email_address")),
        "auth_code": json_as_str_or(config.get("auth_code")),
        "imap_host": json_as_str_or(config.get("imap_host")),
        "imap_port": _as_port(config.get("imap_port"), 993),
        "smtp_host": json_as_str_or(config.get("smtp_host")),
        "smtp_port": _as_port(config.get("smtp_port"), 465),
        "smtp_ssl": json_as_bool(config.get("smtp_ssl"), True),
    }

    if provider != "custom" and provider in EMAIL_PROVIDERS:
        preset = EMAIL_PROVIDERS[provider]
        result["imap_host"] = preset["imap_host"]
        result["imap_port"] = preset["imap_port"]
        result["smtp_host"] = preset["smtp_host"]
        result["smtp_port"] = preset["smtp_port"]
        result["smtp_ssl"] = preset["smtp_ssl"]

    return result


def _message_header(msg: Message, key: str, default: str = "") -> str:
    value = cast("object", msg.get(key, default))
    return value if isinstance(value, str) else default


def _decode_header_value(value: str) -> str:
    """Decode an email header value (handles encoded words)."""
    if not value:
        return ""
    result: list[str] = []
    items = list[object](decode_header(value))
    for item in items:
        if not isinstance(item, tuple):
            continue
        pair = cast("tuple[object, ...]", item)
        if len(pair) < 2:
            continue
        part, charset = pair[0], pair[1]
        encoding = charset if isinstance(charset, str) else "utf-8"
        if isinstance(part, bytes):
            result.append(part.decode(encoding, errors="replace"))
        elif isinstance(part, str):
            result.append(part)
    return "".join(result)


def _extract_body(msg: Message) -> str:
    """Extract the plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback to HTML if no plain text
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    return f"[HTML content]\n{payload.decode(charset, errors='replace')[:2000]}"
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


class AttachmentRefusedError(Exception):
    """An attachment cannot go out, so the message does not; the text is the tool's reply."""


# Raw bytes per read: a whole number of 57-byte groups, so every base64 line of a chunk is full and only the last
# chunk of a file ends in padding.
_CHUNK_BYTES = 57 * 8192
# A message up to this size is built in memory; a larger one rolls over to a temporary file that is deleted on close.
_SPOOL_BYTES = 256 * 1024
_WIRE_POLICY = compat32.clone(linesep="\r\n")


def _attachment_key(agent_prefix: str, rel_path: str) -> str | None:
    """The storage key of an attachment, or None when it is not under the agent's own prefix.

    A key is normalized as every storage key is, then must still sit under the agent's own prefix: ``..`` pops
    segments, so without that check it would reach another agent's files or climb out of the storage root.
    """
    from app.services.storage import normalize_storage_key

    clean_rel = rel_path.replace("\\", "/").strip().lstrip("/")
    storage_key = normalize_storage_key(f"{agent_prefix}/{clean_rel}")
    return storage_key if storage_key.startswith(f"{agent_prefix}/") else None


def _write_base64(out: IO[bytes], chunk: bytes) -> None:
    _ = out.write(base64.encodebytes(chunk).replace(b"\n", b"\r\n"))


def _write_base64_read(out: IO[bytes], stream: BinaryIO, size: int) -> int:
    chunk = stream.read(size)
    _write_base64(out, chunk)
    return len(chunk)


async def _copy_attachment(storage: StorageBackend, key: str, budget: int, out: IO[bytes]) -> int:
    """Append the base64 of the file at `key` to `out` in chunks, and return its size in bytes.

    A file over `budget` raises `StorageReadLimitExceededError` before a byte is read, a missing file or a directory
    `FileNotFoundError`, and a file that changes or comes up short while it is read `StorageReadChangedError`.
    A local file is read through the descriptor the storage opened, whose `fstat` before and after tells whether it
    changed; its version call hashes the whole file. Any other key is read in ranged requests between two version
    lookups that must agree.
    """
    if (opened := await storage.open_local_file(key)) is not None:
        stream, info = opened
        try:
            if info.st_size > budget:
                raise StorageReadLimitExceededError(budget, info.st_size)
            left = info.st_size
            while left:
                wanted = min(_CHUNK_BYTES, left)
                if await AGENT_EMAIL_LANE.run(partial(_write_base64_read, out, stream, wanted)) != wanted:
                    raise StorageReadChangedError(key)
                left -= wanted
            after = await AGENT_EMAIL_LANE.run(partial(os.fstat, stream.fileno()))
            if (after.st_mtime_ns, after.st_size) != (info.st_mtime_ns, info.st_size):
                raise StorageReadChangedError(key)
        finally:
            stream.close()
        return info.st_size
    version = await storage.get_version_bounded(key, budget)
    if not version.exists or version.is_dir:
        raise FileNotFoundError(key)
    for position in range(0, version.size, _CHUNK_BYTES):
        wanted = min(_CHUNK_BYTES, version.size - position)
        chunk = await storage.read_bytes_bounded(key, wanted, offset=position)
        if len(chunk) != wanted:
            raise StorageReadChangedError(key)
        await AGENT_EMAIL_LANE.run(partial(_write_base64, out, chunk))
    if (await storage.get_version(key)).token != version.token:
        raise StorageReadChangedError(key)
    return version.size


async def _write_message(msg: MIMEMultipart, agent_prefix: str, attachments: list[str], out: IO[bytes]) -> None:
    """Write `msg` to `out` as the wire message, each attachment read from storage within `EMAIL_ATTACHMENTS_MAX_BYTES`.

    The email package lays out the whole message (headers, boundaries, RFC 2231 filenames) around a one-line stand-in
    for every attachment body; each stand-in is replaced by the file's base64 as the file is read, so memory is a few
    chunks however large the files are. A missing, unreadable, out-of-workspace, changed or over-budget file raises
    `AttachmentRefusedError` for the first such file in list order; nothing has been sent by then.
    """
    from app.services.storage import get_storage_backend

    storage = get_storage_backend()
    limit = get_settings().EMAIL_ATTACHMENTS_MAX_BYTES
    remaining = limit
    planned: list[tuple[str, str | None, bytes]] = []
    for rel_path in attachments:
        storage_key = _attachment_key(agent_prefix, rel_path)
        stand_in = secrets.token_hex(16)
        part = MIMEBase("application", "octet-stream")
        part.set_payload(stand_in)
        part["Content-Transfer-Encoding"] = "base64"
        part.add_header("Content-Disposition", "attachment", filename=Path(storage_key or rel_path).name)
        msg.attach(part)
        planned.append((rel_path, storage_key, stand_in.encode("ascii")))
    rest = msg.as_bytes(policy=_WIRE_POLICY)
    for rel_path, storage_key, stand_in in planned:
        before, _, rest = rest.partition(stand_in)
        _ = out.write(before)
        if storage_key is None:
            raise AttachmentRefusedError(
                f"❌ Attachment {rel_path} is not a file inside the workspace. Nothing was sent."
            )
        try:
            remaining -= await _copy_attachment(storage, storage_key, remaining, out)
        except StorageReadLimitExceededError:
            raise AttachmentRefusedError(
                f"❌ Attachment {rel_path} takes the message past the {limit}-byte limit on attachments. "
                "Nothing was sent."
            ) from None
        except FileNotFoundError:
            raise AttachmentRefusedError(f"❌ Attachment not found: {rel_path}. Nothing was sent.") from None
        except StorageReadChangedError:
            raise AttachmentRefusedError(
                f"❌ Attachment {rel_path} changed while it was being read. Nothing was sent."
            ) from None
        except Exception as error:
            logger.warning(f"Could not read email attachment {storage_key}: {error}")
            raise AttachmentRefusedError(
                f"❌ Could not read attachment {rel_path}: {str(error)[:200]}. Nothing was sent."
            ) from error
    _ = out.write(rest)


async def send_email(
    config: EmailConfig,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    attachments: list[str] | None = None,
    workspace_path: Path | None = None,
    agent_id: uuid.UUID | None = None,
) -> str:
    """Send an email via SMTP.

    Args:
        config: Resolved email config (from resolve_config)
        to: Recipient email address(es), comma-separated
        subject: Email subject
        body: Email body text
        cc: CC recipients, comma-separated
        attachments: List of workspace-relative file paths to attach. Every file is read from storage; a missing,
            unreadable or out-of-workspace file, a file that changes while it is read, or a total past
            ``EMAIL_ATTACHMENTS_MAX_BYTES`` refuses the send
        workspace_path: Agent workspace directory; its name is the storage prefix when ``agent_id`` is not given
        agent_id: Optional UUID of the agent whose storage prefix the attachments are read from
    """
    cfg = resolve_config(config)
    addr = cfg["email_address"]
    password = cfg["auth_code"]

    if not addr or not password:
        return "❌ Email not configured. Please set email address and authorization code in tool config."

    msg = MIMEMultipart()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = subject
    if cc not in (None, ""):
        msg["Cc"] = cc
    msg["Message-ID"] = make_msgid()
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S %z")

    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        recipients = [r.strip() for r in to.split(",")]
        if cc not in (None, ""):
            recipients += [r.strip() for r in cc.split(",")]

        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES) as outgoing:
            if attachments is not None and attachments and workspace_path is not None:
                try:
                    await _write_message(msg, str(agent_id) if agent_id else workspace_path.name, attachments, outgoing)
                except AttachmentRefusedError as refusal:
                    return str(refusal)
            else:
                _ = outgoing.write(msg.as_bytes(policy=_WIRE_POLICY))
            _ = outgoing.seek(0)
            # Never abandoned on cancel: `outgoing` closes when this block ends, and a half-written DATA must finish.
            _ = await AGENT_EMAIL_LANE.run(
                partial(
                    send_smtp_stream,
                    host=cfg["smtp_host"],
                    port=cfg["smtp_port"],
                    user=addr,
                    password=password,
                    from_addr=addr,
                    to_addrs=recipients,
                    message=outgoing,
                    use_ssl=cfg.get("smtp_ssl", True),
                    timeout=_EXCHANGE_TIMEOUT_SECONDS,
                )
            )

        return f"✅ Email sent to {to}" + (f" (CC: {cc})" if cc not in (None, "") else "")
    except smtplib.SMTPAuthenticationError:
        return "❌ SMTP authentication failed. Please check your email address and authorization code."
    except Exception as e:
        return f"❌ Failed to send email: {str(e)[:200]}"


async def _send_off_loop(cfg: ResolvedEmailConfig, to_addrs: list[str], msg_string: str, socket_timeout: int) -> None:
    """Send a small message on the agent email lane, so its SMTP round trips never hold the event loop."""
    await AGENT_EMAIL_LANE.run(
        partial(
            send_smtp_email,
            host=cfg["smtp_host"],
            port=cfg["smtp_port"],
            user=cfg["email_address"],
            password=cfg["auth_code"],
            from_addr=cfg["email_address"],
            to_addrs=to_addrs,
            msg_string=msg_string,
            use_ssl=cfg.get("smtp_ssl", True),
            timeout=socket_timeout,
        )
    )


@contextmanager
def _imap_session(cfg: ResolvedEmailConfig, folder: str, timeout: int) -> Generator[imaplib.IMAP4_SSL]:
    """A logged-in IMAP connection with `folder` selected read-only, logged out on exit; blocking, for a lane thread."""
    with (
        force_ipv4(),
        imaplib.IMAP4_SSL(
            cfg["imap_host"], cfg["imap_port"], ssl_context=ssl.create_default_context(), timeout=timeout
        ) as mail,
    ):
        _ = mail.login(cfg["email_address"], cfg["auth_code"])
        _ = mail.select(folder, readonly=True)
        yield mail


async def _run_imap[T](exchange: Callable[[], T]) -> T:
    """Run one IMAP exchange on the agent email lane.

    An exchange only reads and closes its own connection within the socket timeouts, so a cancelled caller leaves it
    to finish instead of waiting for it.
    """
    return await AGENT_EMAIL_LANE.run(exchange, abandon_on_cancel=True)


def _read_mailbox(cfg: ResolvedEmailConfig, limit: int, search: str | None, folder: str) -> str:
    """The reply of `read_emails`: its whole IMAP exchange, search to last fetch."""
    with _imap_session(cfg, folder, _EXCHANGE_TIMEOUT_SECONDS) as mail:
        # Search
        if search not in (None, ""):
            _, msg_nums = mail.search(None, search)
        else:
            _, msg_nums = mail.search(None, "ALL")

        raw_ids = msg_nums[0] if msg_nums else None
        if not isinstance(raw_ids, (bytes, bytearray)):
            return "📭 No emails found."
        msg_ids = raw_ids.decode().split()
        if not msg_ids:
            return "📭 No emails found."

        # Get latest N emails
        latest_ids = msg_ids[-limit:]
        latest_ids.reverse()  # Newest first

        results: list[str] = []
        for mid in latest_ids:
            _, msg_data = mail.fetch(mid, "(RFC822)")
            first = msg_data[0] if msg_data else None
            if not isinstance(first, tuple):
                continue
            raw = cast("object", first[1])
            if not isinstance(raw, bytes):
                raise TypeError("IMAP message payload must be bytes")
            msg = email_lib.message_from_bytes(raw)

            from_addr = _decode_header_value(_message_header(msg, "From"))
            subject = _decode_header_value(_message_header(msg, "Subject", "(No subject)"))
            date_str = _message_header(msg, "Date")
            message_id = _message_header(msg, "Message-ID")
            body = _extract_body(msg)
            # Truncate body for readability
            if len(body) > 500:
                body = body[:500] + "..."

            results.append(
                "---\n"
                f"**From:** {from_addr}\n"
                f"**Subject:** {subject}\n"
                f"**Date:** {date_str}\n"
                f"**Message-ID:** {message_id}\n"
                f"**Body:**\n{body}"
            )

        header = f"📬 {len(results)} email(s) from {folder}:\n\n"
        return header + "\n\n".join(results)


async def read_emails(
    config: EmailConfig,
    limit: int = 10,
    search: str | None = None,
    folder: str = "INBOX",
) -> str:
    """Read emails from IMAP mailbox.

    Args:
        config: Resolved email config
        limit: Max number of emails to return
        search: Optional IMAP search criteria (e.g. 'FROM "john"', 'SUBJECT "hello"')
        folder: Mailbox folder (default INBOX)
    """
    cfg = resolve_config(config)
    addr = cfg["email_address"]
    password = cfg["auth_code"]

    if not addr or not password:
        return "❌ Email not configured. Please set email address and authorization code in tool config."

    limit = min(limit, 30)  # Cap at 30

    try:
        return await _run_imap(partial(_read_mailbox, cfg, limit, search, folder))
    except imaplib.IMAP4.error as e:
        err = str(e)
        if "LOGIN" in err.upper() or "AUTH" in err.upper():
            return "❌ IMAP authentication failed. Please check your email address and authorization code."
        return f"❌ IMAP error: {err[:200]}"
    except Exception as e:
        return f"❌ Failed to read emails: {str(e)[:200]}"


def _fetch_original(cfg: ResolvedEmailConfig, message_id: str, folder: str) -> tuple[str, str] | None:
    """The From and decoded Subject of the message with `message_id`, or None when the folder has none."""
    with _imap_session(cfg, folder, _EXCHANGE_TIMEOUT_SECONDS) as mail:
        _, msg_nums = mail.search(None, f'HEADER Message-ID "{message_id}"')
        raw_ids = msg_nums[0] if msg_nums else None
        if not isinstance(raw_ids, (bytes, bytearray)):
            return None
        msg_ids = raw_ids.decode().split()
        if not msg_ids:
            return None

        _, msg_data = mail.fetch(msg_ids[0], "(RFC822)")
        first = msg_data[0] if msg_data else None
        if not isinstance(first, tuple) or len(first) < 2:
            raise TypeError("IMAP message payload is missing")
        raw = cast("object", first[1])
        if not isinstance(raw, bytes):
            raise TypeError("IMAP message payload must be bytes")
        original = email_lib.message_from_bytes(raw)
        return _message_header(original, "From"), _decode_header_value(_message_header(original, "Subject"))


async def reply_email(
    config: EmailConfig,
    message_id: str,
    body: str,
    folder: str = "INBOX",
) -> str:
    """Reply to an email by Message-ID.

    Args:
        config: Resolved email config
        message_id: Message-ID of the email to reply to
        body: Reply body text
        folder: Mailbox folder to search in
    """
    cfg = resolve_config(config)
    addr = cfg["email_address"]
    password = cfg["auth_code"]

    if not addr or not password:
        return "❌ Email not configured."

    try:
        # First, fetch the original email to get From/Subject
        original = await _run_imap(partial(_fetch_original, cfg, message_id, folder))
        if original is None:
            return f"❌ Original email not found with Message-ID: {message_id}"
        original_from, original_subject = original

        # Build reply
        reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

        reply_msg = MIMEMultipart()
        reply_msg["From"] = addr
        reply_to = parseaddr(original_from)[1] or original_from
        reply_msg["To"] = reply_to
        reply_msg["Subject"] = reply_subject
        reply_msg["In-Reply-To"] = message_id
        reply_msg["References"] = message_id
        reply_msg["Message-ID"] = make_msgid()

        reply_msg.attach(MIMEText(body, "plain", "utf-8"))

        # Send
        await _send_off_loop(cfg, [reply_to], reply_msg.as_string(), _EXCHANGE_TIMEOUT_SECONDS)

        return f"✅ Reply sent to {reply_to} (Subject: {reply_subject})"

    except Exception as e:
        return f"❌ Failed to reply: {str(e)[:200]}"


def _count_inbox(cfg: ResolvedEmailConfig) -> int:
    """How many messages the INBOX holds, from one IMAP exchange."""
    with _imap_session(cfg, "INBOX", _PROBE_TIMEOUT_SECONDS) as mail:
        _, msg_nums = mail.search(None, "ALL")
        inbox_ids = msg_nums[0] if msg_nums else None
        return len(inbox_ids.decode().split()) if isinstance(inbox_ids, (bytes, bytearray)) else 0


async def test_connection(config: EmailConfig) -> EmailConnectionResult:
    """Test IMAP and SMTP connections.

    Returns dict with 'ok' (bool), 'imap' (str), 'smtp' (str) status messages.
    """
    cfg = resolve_config(config)
    addr = cfg["email_address"]
    password = cfg["auth_code"]

    if not addr or not password:
        return {"ok": False, "error": "Email address and authorization code are required."}

    result: EmailConnectionResult = {"ok": True, "imap": "", "smtp": ""}

    # Test IMAP
    try:
        count = await _run_imap(partial(_count_inbox, cfg))
        result["imap"] = f"✅ IMAP connected ({count} emails in INBOX)"
    except imaplib.IMAP4.error as e:
        result["ok"] = False
        result["imap"] = f"❌ IMAP failed: {str(e)[:150]}"
    except Exception as e:
        result["ok"] = False
        result["imap"] = f"❌ IMAP error: {str(e)[:150]}"

    # Test SMTP
    try:
        await _send_off_loop(
            cfg,
            [addr],  # Send to self for test
            f"From: {addr}\nTo: {addr}\nSubject: MaraClaw Connection Test\n\nSMTP Connection Successful.",
            _PROBE_TIMEOUT_SECONDS,
        )
        result["smtp"] = "✅ SMTP connected"
    except smtplib.SMTPAuthenticationError:
        result["ok"] = False
        result["smtp"] = "❌ SMTP authentication failed"
    except Exception as e:
        result["ok"] = False
        result["smtp"] = f"❌ SMTP error: {str(e)[:150]}"

    return result

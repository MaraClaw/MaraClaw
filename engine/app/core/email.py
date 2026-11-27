"""Core email utilities for SMTP operations and network compatibility."""

import os
import re
import smtplib
import socket
import ssl
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager, nullcontext, suppress
from typing import IO, Literal
from unittest.mock import patch

from app.core.smtp_policy import PublicSMTP, PublicSMTPSSL


def _ipv4_getaddrinfo(
    host: str | bytes | None,
    port: str | bytes | int | None,
    family: int = 0,
    *socket_args: int,
    **socket_options: int,
) -> list[
    tuple[Literal[socket.AddressFamily.AF_INET], socket.SocketKind, int, str, tuple[str, int]]
    | tuple[
        Literal[socket.AddressFamily.AF_INET6],
        socket.SocketKind,
        int,
        str,
        tuple[str, int, int, int] | tuple[int, bytes],
    ]
]:
    """Wrapper that forces AF_INET (IPv4) to avoid IPv6 failures in Docker."""
    _ = family
    ipv4_addresses: list[
        tuple[Literal[socket.AddressFamily.AF_INET], socket.SocketKind, int, str, tuple[str, int]]
        | tuple[
            Literal[socket.AddressFamily.AF_INET6],
            socket.SocketKind,
            int,
            str,
            tuple[str, int, int, int] | tuple[int, bytes],
        ]
    ] = []
    for _, socket_type, protocol, canonical_name, socket_address in _original_getaddrinfo(
        host, port, socket.AF_INET, *socket_args, **socket_options
    ):
        address = socket_address[0]
        resolved_port = socket_address[1]
        if isinstance(address, str) and isinstance(resolved_port, int):
            ipv4_addresses.append((socket.AF_INET, socket_type, protocol, canonical_name, (address, resolved_port)))
    return ipv4_addresses


_original_getaddrinfo = socket.getaddrinfo
_ipv4_patch = patch.object(socket, "getaddrinfo", _ipv4_getaddrinfo)
_ipv4_lock = threading.Lock()
_ipv4_users = 0


@contextmanager
def force_ipv4():
    """Context manager that forces all socket connections to use IPv4.

    Docker containers often lack IPv6 support, causing [Errno 99] when
    Python picks an AAAA record. This patches socket.getaddrinfo to only
    return IPv4 results while preserving the original hostname for SSL
    certificate verification (SNI).

    The patch is process-wide and shared: the first user installs it and the last one out removes it, so sends that
    overlap in worker threads can leave neither a stale patch nor an unpatched lookup behind.
    """
    global _ipv4_users
    with _ipv4_lock:
        if _ipv4_users == 0:
            _ = _ipv4_patch.start()
        _ipv4_users += 1
    try:
        yield
    finally:
        with _ipv4_lock:
            _ipv4_users -= 1
            if _ipv4_users == 0:
                _ipv4_patch.stop()


@contextmanager
def _smtp_session(
    host: str, port: int, user: str, password: str, use_ssl: bool, timeout: int, public_only: bool
) -> Generator[smtplib.SMTP]:
    """An open, secured and authenticated SMTP connection that is closed with QUIT on exit; modes: `send_smtp_email`."""
    # Public dials own their DNS lookup and pin the socket. Do not patch process DNS.
    smtp_class = PublicSMTP if public_only else smtplib.SMTP
    ssl_class = PublicSMTPSSL if public_only else smtplib.SMTP_SSL
    with nullcontext() if public_only else force_ipv4():
        if use_ssl:
            context = ssl.create_default_context()
            with ssl_class(host, port, context=context, timeout=timeout) as server:
                if user or password:
                    _ = server.login(user, password)
                yield server
        else:
            with smtp_class(host, port, timeout=timeout) as server:
                _ = server.ehlo()

                # Upgrade to STARTTLS only if the server explicitly advertises
                # support.  This prevents crashing on plaintext internal relays
                # that do not support encryption.
                if "starttls" in server.esmtp_features:
                    _ = server.starttls(context=ssl.create_default_context())
                    _ = server.ehlo()

                # Only attempt login when the server supports AUTH and
                # credentials were provided.  Internal network relays often
                # whitelist IPs and do not advertise or accept AUTH.
                if (user or password) and "auth" in server.esmtp_features:
                    _ = server.login(user, password)

                yield server


def send_smtp_email(
    host: str,
    port: int,
    user: str,
    password: str,
    from_addr: str,
    to_addrs: list[str],
    msg_string: str,
    use_ssl: bool = True,
    timeout: int = 15,
    public_only: bool = False,
) -> None:
    """Synchronously send an email via SMTP with IPv4 enforcement.

    Three connection modes are supported depending on ``use_ssl`` and
    server capabilities:

    * ``use_ssl=True``  -- Direct TLS connection (SMTP_SSL, typically port 465).
    * ``use_ssl=False`` -- Plain SMTP with auto-negotiated STARTTLS upgrade.
      If the server advertises STARTTLS support, the connection is upgraded;
      otherwise transmission proceeds in plaintext (suitable for internal
      network relays on port 25).

    Authentication is only attempted when both credentials are provided
    AND the server advertises AUTH support (``use_ssl=False`` path).
    This allows unauthenticated IP-whitelisted internal relays to work.

    Args:
        host: SMTP server host
        port: SMTP server port
        user: SMTP username (may be empty for internal relays)
        password: SMTP password/auth-code (may be empty for internal relays)
        from_addr: Sender email address
        to_addrs: List of recipient email addresses
        msg_string: The full MIME message as a string
        use_ssl: True for direct TLS (port 465), False for plain/STARTTLS
        timeout: Socket timeout in seconds
    """
    with _smtp_session(host, port, user, password, use_ssl, timeout, public_only) as server:
        _ = server.sendmail(from_addr, to_addrs, msg_string)


_SEND_BLOCK_BYTES = 64 * 1024
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")
_LEADING_DOT = re.compile(rb"(?<=\n)\.")


def _data_blocks(message: IO[bytes]) -> Iterator[bytes]:
    """The DATA payload for `message` in bounded blocks, whatever the chunking of the reads.

    Every line break (CRLF, LF or a bare CR) leaves as CRLF, a line that starts with ``.`` gets a second dot
    (RFC 5321 4.5.2), a last line without a break gets one, and ``.`` CRLF ends the payload. A CR that ends one
    block waits for the start of the next, so a CRLF split between two reads is still one break.
    """
    carried_cr = False
    at_line_start = True
    while block := message.read(_SEND_BLOCK_BYTES):
        data = b"\r" + block if carried_cr else block
        carried_cr = data.endswith(b"\r")
        if carried_cr:
            data = data[:-1]
        data = _LINE_BREAK.sub(b"\r\n", data)
        if at_line_start and data.startswith(b"."):
            data = b"." + data
        data = _LEADING_DOT.sub(b"..", data)
        if data:
            at_line_start = data.endswith(b"\n")
            yield data
    if carried_cr:
        yield b"\r\n"
        at_line_start = True
    if not at_line_start:
        yield b"\r\n"
    yield b".\r\n"


def _abort_transaction(server: smtplib.SMTP, code: int) -> None:
    """Leave the session as `smtplib.SMTP.sendmail` does after a refusal: closed after a 421, reset otherwise."""
    if code == 421:
        server.close()
    else:
        with suppress(smtplib.SMTPServerDisconnected):
            _ = server.rset()


def _transmit(
    server: smtplib.SMTP, from_addr: str, to_addrs: list[str], message: IO[bytes]
) -> dict[str, tuple[int, bytes]]:
    """`smtplib.SMTP.sendmail` for a message read from a file: same commands, refusals and exceptions, no copy of it."""
    start = message.tell()
    size = message.seek(0, os.SEEK_END) - start
    _ = message.seek(start)
    server.ehlo_or_helo_if_needed()
    options = [f"size={size}"] if server.does_esmtp and server.has_extn("size") else []
    code, reply = server.mail(from_addr, options)
    if code != 250:
        _abort_transaction(server, code)
        raise smtplib.SMTPSenderRefused(code, reply, from_addr)
    refused: dict[str, tuple[int, bytes]] = {}
    for recipient in to_addrs:
        code, reply = server.rcpt(recipient)
        if code not in (250, 251):
            refused[recipient] = (code, reply)
        if code == 421:
            server.close()
            raise smtplib.SMTPRecipientsRefused(refused)
    if len(refused) == len(to_addrs):
        _abort_transaction(server, code)
        raise smtplib.SMTPRecipientsRefused(refused)
    server.putcmd("data")
    code, reply = server.getreply()
    if code != 354:
        raise smtplib.SMTPDataError(code, reply)
    for block in _data_blocks(message):
        server.send(block)
    code, reply = server.getreply()
    if code != 250:
        _abort_transaction(server, code)
        raise smtplib.SMTPDataError(code, reply)
    return refused


def send_smtp_stream(
    host: str,
    port: int,
    user: str,
    password: str,
    from_addr: str,
    to_addrs: list[str],
    message: IO[bytes],
    use_ssl: bool = True,
    timeout: int = 15,
    public_only: bool = False,
) -> dict[str, tuple[int, bytes]]:
    """Synchronously send the message `message` holds, from its position to its end, in bounded memory.

    The connection, TLS, authentication and IPv4 rules are those of `send_smtp_email`; the message is never held
    whole: it is read in blocks, normalised to CRLF and dot-stuffed on the way out. Refusals raise what
    `smtplib.SMTP.sendmail` raises (all recipients refused, a refused sender, a refused DATA). Recipients the
    server refused while others were accepted are returned, as `sendmail` returns them.
    """
    with _smtp_session(host, port, user, password, use_ssl, timeout, public_only) as server:
        return _transmit(server, from_addr, to_addrs, message)

"""`send_smtp_stream` against a real SMTP server on a localhost socket, and the connection code it shares."""

from __future__ import annotations

import io
import re
import smtplib
import socket
from contextlib import ExitStack
from pathlib import Path

import pytest

from app.core import email as core_email
from app.core.email import force_ipv4, send_smtp_email, send_smtp_stream
from smtp_capture_server import SMTPScript, SMTPServer, serve_smtp

SENDER = "agent@example.test"


def stream_to(
    server: SMTPServer, source: bytes, recipients: list[str] | None = None, *, user: str = "", password: str = ""
) -> dict[str, tuple[int, bytes]]:
    return send_smtp_stream(
        host="127.0.0.1",
        port=server.port,
        user=user,
        password=password,
        from_addr=SENDER,
        to_addrs=recipients or ["to@example.test"],
        message=io.BytesIO(source),
        use_ssl=False,
        timeout=10,
    )


def wire_form(source: bytes) -> bytes:
    """What a conforming client puts on the wire for `source`: CRLF lines, leading dots doubled, closing dot line."""
    lines = re.sub(rb"\r\n|\r|\n", b"\r\n", source)
    stuffed = re.sub(rb"(?m)^\.", b"..", lines)
    return stuffed if stuffed.endswith(b"\r\n") else stuffed + b"\r\n"


SOURCES = {
    "crlf lines": b"first\r\nsecond\r\n",
    "lf lines": b"first\nsecond\n",
    "bare cr": b"first\rsecond\r",
    "mixed breaks": b"a\r\nb\nc\rd\r\n\r\ne",
    "leading dots": b".one\r\n..two\r\nthree\r\n.\r\n",
    "dot after lf": b"x\n.y\n",
    "dot after bare cr": b"x\r.y",
    "one dot": b".",
    "no final break": b"no final break",
    "only breaks": b"\r\n\r\n",
    "single cr": b"\r",
    "single lf": b"\n",
}


@pytest.mark.parametrize("block", [1, 2, 3, 7, 64 * 1024])
@pytest.mark.parametrize("name", SOURCES)
def test_the_payload_is_the_same_wherever_the_reads_split_it(
    monkeypatch: pytest.MonkeyPatch, name: str, block: int
) -> None:
    # Given a source whose line breaks and dots sit on every possible block boundary
    monkeypatch.setattr(core_email, "_SEND_BLOCK_BYTES", block)
    source = SOURCES[name]

    # When it is turned into the DATA payload
    payload = b"".join(core_email._data_blocks(io.BytesIO(source)))

    # Then it is CRLF only, dot-stuffed, ends in a line break and the closing dot line, and does not depend on the block
    assert payload == wire_form(source) + b".\r\n"


def test_an_empty_source_is_just_the_closing_dot_line() -> None:
    assert b"".join(core_email._data_blocks(io.BytesIO(b""))) == b".\r\n"


def test_a_message_reaches_the_server_with_its_exact_bytes(tmp_path: Path) -> None:
    # Given a message with binary-safe base64 lines, lines that start with a dot, LF-only and bare CR breaks
    source = b"Subject: dots\r\n\r\n.hidden\n..twice\rtext\r\n.\r\nlast line"
    with serve_smtp(tmp_path) as server:
        # When it is sent
        refused = stream_to(server, source)
        session = server.sessions.get(timeout=10)

        # Then the server stored every line as sent, the dots are doubled on the wire and every break is CRLF
        assert refused == {}
        assert session.sender == SENDER
        assert session.accepted == ["to@example.test"]
        assert session.bad_line_endings == 0
        assert session.wire_bytes() == wire_form(source)
        assert session.message_bytes() == re.sub(rb"\r\n|\r|\n", b"\r\n", source) + b"\r\n"
        assert b"\r\n..hidden\r\n" in session.wire_bytes()


def test_the_message_is_announced_with_its_size(tmp_path: Path) -> None:
    source = b"Subject: size\r\n\r\nbody\r\n"
    with serve_smtp(tmp_path) as server:
        _ = stream_to(server, source)

        session = server.sessions.get(timeout=10)
    assert f"mail from:<{SENDER}> size={len(source)}" in [command.lower() for command in session.commands]


def test_a_message_over_the_servers_size_limit_is_refused_before_any_recipient(tmp_path: Path) -> None:
    with serve_smtp(tmp_path, SMTPScript(size_limit=10)) as server:
        with pytest.raises(smtplib.SMTPSenderRefused) as refusal:
            _ = stream_to(server, b"x" * 11)

        session = server.sessions.get(timeout=10)
    assert refusal.value.smtp_code == 552
    assert not any(command.startswith(("RCPT", "DATA")) for command in session.commands)


def test_a_refused_recipient_does_not_stop_the_others(tmp_path: Path) -> None:
    # Given a server that refuses one of three recipients
    recipients = ["a@example.test", "gone@example.test", "c@example.test"]
    with serve_smtp(tmp_path, SMTPScript(refused_recipients=frozenset({"gone@example.test"}))) as server:
        # When the message goes to all three
        refused = stream_to(server, b"Subject: s\r\n\r\nbody\r\n", recipients)
        session = server.sessions.get(timeout=10)

        # Then it is delivered to the other two and the refusal is returned, as smtplib's sendmail returns it
        assert list(refused) == ["gone@example.test"]
        assert refused["gone@example.test"][0] == 550
        assert session.accepted == ["a@example.test", "c@example.test"]
        assert session.message_bytes() == b"Subject: s\r\n\r\nbody\r\n"


def test_a_send_no_recipient_accepts_fails_and_never_starts_the_data_command(tmp_path: Path) -> None:
    with serve_smtp(tmp_path, SMTPScript(refused_recipients=frozenset({"a@example.test", "b@example.test"}))) as server:
        with pytest.raises(smtplib.SMTPRecipientsRefused) as refusal:
            _ = stream_to(server, b"body\r\n", ["a@example.test", "b@example.test"])

        session = server.sessions.get(timeout=10)
    assert sorted(refusal.value.recipients) == ["a@example.test", "b@example.test"]
    assert "DATA" not in session.commands
    assert "RSET" in session.commands


def test_a_message_the_server_rejects_after_the_data_surfaces_as_a_failed_send(tmp_path: Path) -> None:
    with serve_smtp(tmp_path, SMTPScript(data_reply="554 transaction failed")) as server:
        with pytest.raises(smtplib.SMTPDataError) as refusal:
            _ = stream_to(server, b"Subject: s\r\n\r\nbody\r\n")

        _ = server.sessions.get(timeout=10)
    assert refusal.value.smtp_code == 554


def test_a_message_is_sent_from_where_its_file_is_positioned(tmp_path: Path) -> None:
    message = io.BytesIO(b"ignored prefix" + b"Subject: s\r\n\r\nbody\r\n")
    _ = message.seek(len(b"ignored prefix"))
    with serve_smtp(tmp_path) as server:
        _ = send_smtp_stream(
            host="127.0.0.1",
            port=server.port,
            user="",
            password="",
            from_addr=SENDER,
            to_addrs=["to@example.test"],
            message=message,
            use_ssl=False,
        )

        session = server.sessions.get(timeout=10)
    assert session.message_bytes() == b"Subject: s\r\n\r\nbody\r\n"


def test_credentials_are_offered_only_when_the_server_advertises_authentication(tmp_path: Path) -> None:
    with serve_smtp(tmp_path) as server:
        _ = stream_to(server, b"body\r\n", user="agent", password="secret")

        session = server.sessions.get(timeout=10)
    assert not any(command.startswith("AUTH") for command in session.commands)


def test_system_email_goes_through_the_same_connection_with_unchanged_behaviour(tmp_path: Path) -> None:
    # Given a message string with LF breaks, as the system email service builds it
    with serve_smtp(tmp_path) as server:
        # When it is sent with send_smtp_email
        send_smtp_email(
            host="127.0.0.1",
            port=server.port,
            user="",
            password="",
            from_addr=SENDER,
            to_addrs=["a@example.test", "b@example.test"],
            msg_string="Subject: system\n\n.dot\nbody",
            use_ssl=False,
            timeout=10,
        )
        session = server.sessions.get(timeout=10)

        # Then the server gets the same envelope and the CRLF, dot-stuffed message smtplib has always sent
        assert session.accepted == ["a@example.test", "b@example.test"]
        assert session.wire_bytes() == b"Subject: system\r\n\r\n..dot\r\nbody\r\n"


def test_overlapping_ipv4_scopes_restore_the_lookup_only_when_the_last_one_ends() -> None:
    # Given the lookup before any scope, and two scopes that overlap rather than nest
    original = socket.getaddrinfo
    first, second = ExitStack(), ExitStack()
    _ = first.enter_context(force_ipv4())
    patched = socket.getaddrinfo
    _ = second.enter_context(force_ipv4())

    # When the first one ends while the second is still open
    first.close()

    # Then the lookup stays forced to IPv4 until the second ends, and is the original afterwards
    assert patched is not original
    assert socket.getaddrinfo is patched
    second.close()
    assert socket.getaddrinfo is original

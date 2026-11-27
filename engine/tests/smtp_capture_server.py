"""A real SMTP server on a localhost socket that keeps what a client sent, byte for byte.

`serve_smtp(directory)` listens on 127.0.0.1 and puts one `Session` on `SMTPServer.sessions` when a connection ends, so
a test waits for exactly that event (`sessions.get(timeout=...)`) instead of sleeping. A DATA payload is written to two
files in `directory` as it arrives and never held whole, so a test can measure the memory of the code that sends it and
read the files afterwards. The server shares no code with the sender: it splits the payload into lines itself, undoes
the dot-stuffing itself, and counts the lines that do not end in CRLF.

The server speaks plain SMTP (no STARTTLS, no AUTH) and advertises SIZE. `SMTPScript` makes it refuse recipients, a
sender over a size limit, or the message, or hold its answer to the message until the test sets `hold_reply`
(`SMTPServer.payload_received` says when the whole payload has arrived). Commands are recorded with their verb in upper
case, and `SMTPServer.opened` lists every connection from its greeting on, so "no connection was made" is an exact claim.
"""

import re
import socketserver
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import override


@dataclass(frozen=True)
class SMTPScript:
    """What the server answers instead of its usual 250."""

    refused_recipients: frozenset[str] = frozenset()
    size_limit: int = 0
    data_reply: str = "250 queued"
    hold_reply: threading.Event | None = None


@dataclass
class Session:
    """One connection: its commands, the envelope it was accepted with, and the payload of its DATA command."""

    commands: list[str] = field(default_factory=list)
    sender: str = ""
    accepted: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    wire_path: Path | None = None
    message_path: Path | None = None
    bad_line_endings: int = 0

    def wire_bytes(self) -> bytes:
        """The payload as it crossed the socket: dot-stuffed, CRLF lines, without the closing dot line."""
        assert self.wire_path is not None
        return self.wire_path.read_bytes()

    def message_bytes(self) -> bytes:
        """The payload as a mail store keeps it: the dot-stuffing undone."""
        assert self.message_path is not None
        return self.message_path.read_bytes()


@dataclass
class SMTPServer:
    port: int
    sessions: Queue[Session]
    opened: list[Session]
    payload_received: threading.Event


def _unstuffed(line: bytes) -> bytes:
    return line[1:] if line.startswith(b"..") else line


@contextmanager
def serve_smtp(directory: Path, script: SMTPScript | None = None) -> Generator[SMTPServer]:
    script = script or SMTPScript()
    sessions: Queue[Session] = Queue()
    opened: list[Session] = []
    payload_received = threading.Event()
    received = 0

    class Handler(socketserver.StreamRequestHandler):
        @override
        def handle(self) -> None:
            self.request.settimeout(30)
            session = Session()
            opened.append(session)
            try:
                self._converse(session)
            finally:
                sessions.put(session)

        def _say(self, reply: str) -> None:
            self.wfile.write(reply.encode("ascii") + b"\r\n")
            self.wfile.flush()

        def _converse(self, session: Session) -> None:
            self._say("220 capture ESMTP ready")
            while line := self.rfile.readline():
                verb, _, rest = line.decode("ascii", "replace").rstrip("\r\n").partition(" ")
                session.commands.append(f"{verb.upper()} {rest}".strip())
                command = verb.upper()
                if command in ("EHLO", "HELO"):
                    self._say(f"250-capture\r\n250 SIZE {script.size_limit}")
                elif command == "MAIL":
                    declared = re.search(r"(?i)\bSIZE=(\d+)", rest)
                    if script.size_limit and declared and int(declared.group(1)) > script.size_limit:
                        self._say("552 message size exceeds the limit")
                        continue
                    session.sender = rest.partition(":")[2].split()[0].strip("<>")
                    self._say("250 sender ok")
                elif command == "RCPT":
                    recipient = rest.partition(":")[2].split()[0].strip("<>")
                    if recipient in script.refused_recipients:
                        session.refused.append(recipient)
                        self._say("550 no such user")
                    else:
                        session.accepted.append(recipient)
                        self._say("250 recipient ok")
                elif command == "DATA":
                    self._say("354 end data with <CR><LF>.<CR><LF>")
                    self._receive(session)
                    if script.hold_reply is not None:
                        assert script.hold_reply.wait(timeout=30)
                    self._say(script.data_reply)
                elif command == "QUIT":
                    self._say("221 bye")
                    return
                else:
                    self._say("250 ok")

        def _receive(self, session: Session) -> None:
            nonlocal received
            received += 1
            session.wire_path = directory / f"wire-{received}.bin"
            session.message_path = directory / f"message-{received}.bin"
            with session.wire_path.open("wb") as wire, session.message_path.open("wb") as message:
                while line := self.rfile.readline():
                    if line == b".\r\n":
                        payload_received.set()
                        return
                    if not line.endswith(b"\r\n") or line.count(b"\r") != 1:
                        session.bad_line_endings += 1
                    _ = wire.write(line)
                    _ = message.write(_unstuffed(line))

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield SMTPServer(server.server_address[1], sessions, opened, payload_received)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()

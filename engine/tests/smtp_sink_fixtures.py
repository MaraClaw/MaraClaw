import socketserver
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesParser
from queue import Queue
from typing import override

import pytest


@dataclass(frozen=True)
class SMTPSink:
    port: int
    messages: Queue[Message]


@pytest.fixture
def smtp_sink() -> Iterator[SMTPSink]:
    messages: Queue[Message] = Queue()

    class Handler(socketserver.StreamRequestHandler):
        @override
        def handle(self) -> None:
            self.request.settimeout(5)
            self.wfile.write(b"220 sink ESMTP ready\r\n")
            while line := self.rfile.readline():
                command = line.split(b" ", 1)[0].strip().upper()
                if command in (b"EHLO", b"HELO"):
                    self.wfile.write(b"250-sink\r\n250 SIZE 1048576\r\n")
                elif command == b"DATA":
                    self.wfile.write(b"354 End with dot\r\n")
                    data = bytearray()
                    while chunk := self.rfile.readline():
                        if chunk == b".\r\n":
                            break
                        data.extend(chunk.removeprefix(b".") if chunk.startswith(b"..") else chunk)
                    messages.put(BytesParser().parsebytes(bytes(data)))
                    self.wfile.write(b"250 accepted\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 accepted\r\n")

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield SMTPSink(server.server_address[1], messages)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()

"""A loopback S3 wire server for storage tests: real botocore requests, scriptable provider faults.

It speaks just the path-style calls the storage backend makes (PutObject with ``If-None-Match``, HeadObject,
GetObject, DeleteObject, DeleteObjects, ListObjectsV2) and records every call, so tests can assert both what was
stored and whether the provider was reached at all.
"""

import hashlib
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape, unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, override
from urllib.parse import parse_qs, unquote, urlsplit

type PutFault = Literal["reject_403", "reject_500", "apply_then_500", "apply_then_drop"]
type DeleteFault = Literal["reject_500", "apply_then_500"]
PAGE_SIZE = 1000


@dataclass
class Gate:
    """Hold one PutObject after it arrived until the test releases it."""

    arrived: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    apply_before_hold: bool = False


@dataclass
class S3State:
    objects: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    put_faults: dict[str, PutFault] = field(default_factory=dict)
    gates: dict[str, Gate] = field(default_factory=dict)
    refused_deletes: set[str] = field(default_factory=set)
    delete_faults: dict[str, DeleteFault] = field(default_factory=dict)
    multi_delete_status: int = 200
    lock: threading.Lock = field(default_factory=threading.Lock)

    def count(self, method: str, key_prefix: str = "") -> int:
        return sum(1 for verb, key in self.calls if verb == method and key.startswith(key_prefix))


def _etag(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _decode_aws_chunked(raw: bytes) -> bytes:
    body, position = bytearray(), 0
    while position < len(raw):
        header_end = raw.index(b"\r\n", position)
        size = int(raw[position:header_end].split(b";")[0], 16)
        if size == 0:
            break
        body += raw[header_end + 2 : header_end + 2 + size]
        position = header_end + 2 + size + 2
    return bytes(body)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _state(self) -> S3State:
        assert isinstance(self.server, S3Server)
        return self.server.state

    @override
    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(
        self, status: int, body: bytes = b"", headers: dict[str, str] | None = None, length: int | None = None
    ) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        if self.command != "HEAD":
            _ = self.wfile.write(body)

    def _error(self, status: int, code: str) -> None:
        body = f"<Error><Code>{code}</Code><Message>{code}</Message></Error>".encode()
        self._reply(status, body, {"Content-Type": "application/xml"})

    def _target(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        _, _, key = unquote(parts.path).lstrip("/").partition("/")
        return key, parse_qs(parts.query, keep_blank_values=True)

    def _read_body(self) -> bytes:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        streaming = "aws-chunked" in self.headers.get("Content-Encoding", "")
        return (
            _decode_aws_chunked(raw)
            if streaming or "STREAMING" in self.headers.get("x-amz-content-sha256", "")
            else raw
        )

    def do_PUT(self) -> None:
        key, _ = self._target()
        data, state = self._read_body(), self._state()
        with state.lock:
            state.calls.append(("PUT", key))
            fault, gate = state.put_faults.get(key), state.gates.get(key)
            occupied = key in state.objects
        if self.headers.get("If-None-Match") == "*" and occupied:
            self._error(412, "PreconditionFailed")
            return
        if fault == "reject_403":
            self._error(403, "AccessDenied")
            return
        if fault == "reject_500":
            self._error(500, "InternalError")
            return
        if gate is not None and not gate.apply_before_hold:
            gate.arrived.set()
            _ = gate.release.wait(30)
        with state.lock:
            state.objects[key] = data
        if gate is not None and gate.apply_before_hold:
            gate.arrived.set()
            _ = gate.release.wait(30)
        if fault == "apply_then_500":
            self._error(500, "InternalError")
        elif fault == "apply_then_drop":
            self.close_connection = True
        else:
            self._reply(200, headers={"ETag": f'"{_etag(data)}"'})

    def do_HEAD(self) -> None:
        key, _ = self._target()
        state = self._state()
        with state.lock:
            state.calls.append(("HEAD", key))
            data = state.objects.get(key)
        if data is None:
            self._reply(404)
            return
        modified = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self._reply(200, b"", {"ETag": f'"{_etag(data)}"', "Last-Modified": modified}, length=len(data))

    def do_GET(self) -> None:
        key, query = self._target()
        state = self._state()
        if "list-type" not in query:
            with state.lock:
                state.calls.append(("GET", key))
                data = state.objects.get(key)
            if data is None:
                self._error(404, "NoSuchKey")
            else:
                self._reply(200, data, {"ETag": f'"{_etag(data)}"'})
            return
        prefix = query.get("prefix", [""])[0]
        after = query.get("continuation-token", [""])[0]
        with state.lock:
            state.calls.append(("LIST", prefix))
            keys = sorted(k for k in state.objects if k.startswith(prefix) and k > after)
            sizes = {k: len(state.objects[k]) for k in keys[:PAGE_SIZE]}
        page, truncated = keys[:PAGE_SIZE], len(keys) > PAGE_SIZE
        contents = "".join(
            f"<Contents><Key>{escape(k)}</Key><Size>{sizes[k]}</Size><ETag>&quot;x&quot;</ETag></Contents>"
            for k in page
        )
        token = f"<NextContinuationToken>{escape(page[-1])}</NextContinuationToken>" if truncated else ""
        xml = (
            f"<ListBucketResult><Name>b</Name><Prefix>{escape(prefix)}</Prefix><KeyCount>{len(page)}</KeyCount>"
            f"<IsTruncated>{str(truncated).lower()}</IsTruncated>{token}{contents}</ListBucketResult>"
        )
        self._reply(200, xml.encode(), {"Content-Type": "application/xml"})

    def do_DELETE(self) -> None:
        key, _ = self._target()
        state = self._state()
        with state.lock:
            state.calls.append(("DELETE", key))
            fault = state.delete_faults.get(key)
            refused = key in state.refused_deletes
            if not refused and fault != "reject_500":
                _ = state.objects.pop(key, None)
        if refused:
            self._error(403, "AccessDenied")
        elif fault is not None:
            self._error(500, "InternalError")
        else:
            self._reply(204)

    def do_POST(self) -> None:
        _, query = self._target()
        body, state = self._read_body().decode(), self._state()
        if "delete" not in query:
            self._error(400, "InvalidRequest")
            return
        if state.multi_delete_status != 200:
            self._error(state.multi_delete_status, "InternalError")
            return
        deleted, errors = [], []
        with state.lock:
            for key in (unescape(match) for match in re.findall(r"<Key>(.*?)</Key>", body)):
                state.calls.append(("MULTIDELETE", key))
                if key in state.refused_deletes:
                    errors.append(
                        f"<Error><Key>{escape(key)}</Key><Code>AccessDenied</Code><Message>no</Message></Error>"
                    )
                else:
                    _ = state.objects.pop(key, None)
                    deleted.append(f"<Deleted><Key>{escape(key)}</Key></Deleted>")
        xml = f"<DeleteResult>{''.join(deleted)}{''.join(errors)}</DeleteResult>"
        self._reply(200, xml.encode(), {"Content-Type": "application/xml"})


class S3Server(ThreadingHTTPServer):
    daemon_threads = True
    state: S3State

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = S3State()
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(10)

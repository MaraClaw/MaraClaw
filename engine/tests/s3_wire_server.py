"""A loopback S3 wire server for storage tests: real botocore requests, scriptable provider faults.

It speaks just the path-style calls the storage backend makes (PutObject with ``If-None-Match``, HeadObject,
GetObject, DeleteObject, DeleteObjects, ListObjectsV2) and records every call, so tests can assert both what was
stored and whether the provider was reached at all. It also keeps the bucket's ``?versioning`` status and
``?lifecycle`` document (``state.versioning`` and ``state.lifecycle``), recorded as ``?versioning`` / ``?lifecycle`` calls.

GetObject answers a single ``Range`` as S3 does (``_select_range``) and records the header of each ranged GET as a
``RANGE`` call next to its ``GET``; ``state.ranges(key)`` lists them in request order.

``state.throttles`` scripts a brownout: a ``Throttle`` keyed by (verb, key) refuses the next ``remaining`` GET, HEAD, PUT
or DELETE (key) or LIST (prefix) requests with a throttling error and no effect, then serves them normally. Every refused request
is still recorded in ``state.calls``, so a test counts the attempts the client made.
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
type RangeOutcome = tuple[int, int] | Literal["ignored", "unsatisfiable"]
PAGE_SIZE = 1000
BUCKET_CONFIGS = ("versioning", "lifecycle")


@dataclass
class Gate:
    """Hold one PutObject after it arrived until the test releases it."""

    arrived: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    apply_before_hold: bool = False


@dataclass
class Throttle:
    """Answer the next `remaining` matching requests with a throttling error and no effect, then serve them normally."""

    status: int = 503
    code: str = "SlowDown"
    remaining: int = 1


@dataclass
class S3State:
    objects: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    put_faults: dict[str, PutFault] = field(default_factory=dict)
    gates: dict[str, Gate] = field(default_factory=dict)
    refused_deletes: set[str] = field(default_factory=set)
    delete_faults: dict[str, DeleteFault] = field(default_factory=dict)
    throttles: dict[tuple[str, str], Throttle] = field(default_factory=dict)
    multi_delete_status: int = 200
    versioning: str = ""
    lifecycle: bytes | None = None
    refused_bucket_requests: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def count(self, method: str, key_prefix: str = "") -> int:
        return sum(1 for verb, key in self.calls if verb == method and key.startswith(key_prefix))

    def ranges(self, key: str) -> list[str]:
        """The ``Range`` header of every ranged GET of ``key``, in request order (a whole-object GET adds none)."""
        recorded = (value.partition("@") for verb, value in self.calls if verb == "RANGE")
        return [header for header, _, target in recorded if target == key]


def _etag(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _select_range(header: str, size: int) -> RangeOutcome:
    """The inclusive byte span a ``Range`` header selects of a ``size``-byte object, resolved the way S3 does.

    A start at or past the end (any range on an empty object, or ``bytes=-0``) is ``"unsatisfiable"``; an end past the
    end is clamped and a suffix longer than the object selects all of it. A header S3 does not serve as a range (more
    than one range, another unit, text that is not a range, an end before the start) is ``"ignored"``: the whole object
    comes back with 200.
    """
    parsed = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if parsed is None or not any(parsed.groups()):
        return "ignored"
    start, end = parsed.groups()
    if not start:
        suffix = int(end)
        return "unsatisfiable" if suffix == 0 or size == 0 else (max(size - suffix, 0), size - 1)
    first = int(start)
    if end and int(end) < first:
        return "ignored"
    return "unsatisfiable" if first >= size else (first, min(int(end), size - 1) if end else size - 1)


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

    def _error(self, status: int, code: str, headers: dict[str, str] | None = None) -> None:
        body = f"<Error><Code>{code}</Code><Message>{code}</Message></Error>".encode()
        self._reply(status, body, {"Content-Type": "application/xml", **(headers or {})})

    def _throttled(self, verb: str, key: str) -> bool:
        """Refuse this request when a scripted throttle for it has attempts left."""
        state = self._state()
        with state.lock:
            throttle = state.throttles.get((verb, key))
            if throttle is None or throttle.remaining < 1:
                return False
            throttle.remaining -= 1
            status, code = throttle.status, throttle.code
        self._error(status, code)
        return True

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

    def _bucket_config(self, key: str, query: dict[str, list[str]]) -> str | None:
        """The bucket-level subresource (``versioning`` or ``lifecycle``) a request addresses, if any."""
        return None if key else next((name for name in BUCKET_CONFIGS if name in query), None)

    def _get_bucket_config(self, config: str) -> None:
        state = self._state()
        with state.lock:
            state.calls.append(("GET", f"?{config}"))
            refused = config in state.refused_bucket_requests
            versioning, lifecycle = state.versioning, state.lifecycle
        if refused:
            self._error(403, "AccessDenied")
        elif config == "versioning":
            status = f"<Status>{escape(versioning)}</Status>" if versioning else ""
            xml = f"<VersioningConfiguration>{status}</VersioningConfiguration>"
            self._reply(200, xml.encode(), {"Content-Type": "application/xml"})
        elif lifecycle is None:
            self._error(404, "NoSuchLifecycleConfiguration")
        else:
            self._reply(200, lifecycle, {"Content-Type": "application/xml"})

    def _put_bucket_config(self, config: str) -> None:
        body, state = self._read_body(), self._state()
        with state.lock:
            state.calls.append(("PUT", f"?{config}"))
            refused = config in state.refused_bucket_requests
            if not refused and config == "lifecycle":
                state.lifecycle = body
            elif not refused:
                status = re.search(r"<Status>(.*?)</Status>", body.decode())
                state.versioning = unescape(status.group(1)) if status else ""
        if refused:
            self._error(403, "AccessDenied")
        else:
            self._reply(200)

    def do_PUT(self) -> None:
        key, query = self._target()
        config = self._bucket_config(key, query)
        if config is not None:
            self._put_bucket_config(config)
            return
        data, state = self._read_body(), self._state()
        source = self.headers.get("x-amz-copy-source")
        if source is not None:
            self._copy_object(key, unquote(source).lstrip("/").partition("/")[2])
            return
        with state.lock:
            state.calls.append(("PUT", key))
            fault, gate = state.put_faults.get(key), state.gates.get(key)
            occupied = key in state.objects
        if self._throttled("PUT", key):
            return
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
            state.content_types[key] = self.headers.get("Content-Type", "")
        if gate is not None and gate.apply_before_hold:
            gate.arrived.set()
            _ = gate.release.wait(30)
        if fault == "apply_then_500":
            self._error(500, "InternalError")
        elif fault == "apply_then_drop":
            self.close_connection = True
        else:
            self._reply(200, headers={"ETag": f'"{_etag(data)}"'})

    def _copy_object(self, key: str, source: str) -> None:
        """CopyObject: the bytes move inside the provider; ``x-amz-copy-source-if-match`` must equal the source's ETag."""
        state = self._state()
        wanted = self.headers.get("x-amz-copy-source-if-match", "").strip('"')
        with state.lock:
            state.calls.append(("COPY", key))
            fault, data = state.put_faults.get(key), state.objects.get(source)
            if data is not None and (not wanted or wanted == _etag(data)) and fault != "reject_500":
                state.objects[key] = data
                state.content_types[key] = state.content_types.get(source, "")
        if data is None:
            self._error(404, "NoSuchKey")
        elif wanted and wanted != _etag(data):
            self._error(412, "PreconditionFailed")
        elif fault is not None:
            self._error(500, "InternalError")
        else:
            xml = f"<CopyObjectResult><ETag>&quot;{_etag(data)}&quot;</ETag></CopyObjectResult>"
            self._reply(200, xml.encode(), {"Content-Type": "application/xml"})

    def do_HEAD(self) -> None:
        key, _ = self._target()
        state = self._state()
        with state.lock:
            state.calls.append(("HEAD", key))
            data = state.objects.get(key)
        if self._throttled("HEAD", key):
            return
        if data is None:
            self._reply(404)
            return
        modified = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self._reply(200, b"", {"ETag": f'"{_etag(data)}"', "Last-Modified": modified}, length=len(data))

    def do_GET(self) -> None:
        key, query = self._target()
        state = self._state()
        config = self._bucket_config(key, query)
        if config is not None:
            self._get_bucket_config(config)
            return
        if "list-type" not in query:
            requested = self.headers.get("Range")
            with state.lock:
                state.calls.append(("GET", key))
                if requested is not None:
                    state.calls.append(("RANGE", f"{requested}@{key}"))
                data = state.objects.get(key)
            if self._throttled("GET", key):
                return
            if data is None:
                self._error(404, "NoSuchKey")
            else:
                self._serve_object(data, requested)
            return
        prefix = query.get("prefix", [""])[0]
        after = query.get("continuation-token", [""])[0]
        with state.lock:
            state.calls.append(("LIST", prefix))
            keys = sorted(k for k in state.objects if k.startswith(prefix) and k > after)
            listed = {k: state.objects[k] for k in keys[:PAGE_SIZE]}
        if self._throttled("LIST", prefix):
            return
        page, truncated = keys[:PAGE_SIZE], len(keys) > PAGE_SIZE
        contents = "".join(
            f"<Contents><Key>{escape(k)}</Key><Size>{len(listed[k])}</Size>"
            f"<ETag>&quot;{_etag(listed[k])}&quot;</ETag></Contents>"
            for k in page
        )
        token = f"<NextContinuationToken>{escape(page[-1])}</NextContinuationToken>" if truncated else ""
        xml = (
            f"<ListBucketResult><Name>b</Name><Prefix>{escape(prefix)}</Prefix><KeyCount>{len(page)}</KeyCount>"
            f"<IsTruncated>{str(truncated).lower()}</IsTruncated>{token}{contents}</ListBucketResult>"
        )
        self._reply(200, xml.encode(), {"Content-Type": "application/xml"})

    def _serve_object(self, data: bytes, requested: str | None) -> None:
        headers = {"ETag": f'"{_etag(data)}"'}
        selected = "ignored" if requested is None else _select_range(requested, len(data))
        if selected == "unsatisfiable":
            self._error(416, "InvalidRange", {"Content-Range": f"bytes */{len(data)}"})
        elif selected == "ignored":
            self._reply(200, data, headers)
        else:
            first, last = selected
            self._reply(206, data[first : last + 1], {**headers, "Content-Range": f"bytes {first}-{last}/{len(data)}"})

    def do_DELETE(self) -> None:
        key, _ = self._target()
        state = self._state()
        with state.lock:
            state.calls.append(("DELETE", key))
        if self._throttled("DELETE", key):
            return
        with state.lock:
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
            objects = re.findall(r"<Object>(.*?)</Object>", body, re.DOTALL)
            state.calls.append(("DELETEOBJECTS", str(len(objects))))
            for entry in objects:
                key = unescape(next(iter(re.findall(r"<Key>(.*?)</Key>", entry)), ""))
                etag = re.search(r"<ETag>(.*?)</ETag>", entry)
                state.calls.append(("MULTIDELETE", key))
                code = ""
                if key in state.refused_deletes:
                    code = "AccessDenied"
                elif etag is not None and key not in state.objects:
                    code = "NoSuchKey"
                elif etag is not None and unescape(etag.group(1)).strip('"') != _etag(state.objects[key]):
                    code = "PreconditionFailed"
                if code:
                    errors.append(f"<Error><Key>{escape(key)}</Key><Code>{code}</Code><Message>no</Message></Error>")
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

"""`file_response`: validators, single byte ranges, conditional requests and descriptor ownership."""

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import formatdate
from pathlib import Path
from typing import BinaryIO

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.responses import StreamingResponse

from app.services.storage_runtime.file_stream import file_response

BODY = b"0123456789"
DISPOSITION = 'attachment; filename="report.txt"'


@dataclass
class Served:
    """A route that serves `path` with `file_response`, opening it afresh for every request."""

    client: TestClient
    path: Path

    @property
    def etag(self) -> str:
        info = self.path.stat()
        return f'"{info.st_mtime_ns:x}-{info.st_size:x}"'

    @property
    def modified(self) -> str:
        return formatdate(self.path.stat().st_mtime, usegmt=True)


@pytest.fixture
def served(tmp_path: Path) -> Served:
    target = tmp_path / "report.txt"
    _ = target.write_bytes(BODY)
    app = FastAPI()

    @app.get("/file")
    def serve(request: Request) -> Response:
        stream = target.open("rb")
        return file_response(
            request.headers,
            stream,
            os.fstat(stream.fileno()),
            media_type="text/plain",
            headers={"Content-Disposition": DISPOSITION},
        )

    return Served(TestClient(app), target)


def open_file(path: Path) -> tuple[BinaryIO, os.stat_result]:
    stream = path.open("rb")
    return stream, os.fstat(stream.fileno())


def test_full_response_carries_validators_and_caller_headers(served: Served):
    response = served.client.get("/file")

    assert (response.status_code, response.content) == (200, BODY)
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["etag"] == served.etag
    assert response.headers["last-modified"] == served.modified
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["content-length"] == str(len(BODY))
    assert response.headers["content-disposition"] == DISPOSITION
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize("condition", ["*", "{etag}", "W/{etag}", '"other", {etag}', 'W/"other", W/{etag}'])
def test_matching_if_none_match_answers_304_without_a_body(served: Served, condition: str):
    response = served.client.get("/file", headers={"If-None-Match": condition.format(etag=served.etag)})

    assert (response.status_code, response.content) == (304, b"")
    assert "content-length" not in response.headers
    assert response.headers["etag"] == served.etag
    assert response.headers["last-modified"] == served.modified
    assert response.headers["cache-control"] == "private, no-cache"


@pytest.mark.parametrize("with_modified_since", [False, True])
def test_other_if_none_match_sends_the_file_and_outranks_if_modified_since(served: Served, with_modified_since: bool):
    headers = {"If-None-Match": '"other"'}
    if with_modified_since:
        headers["If-Modified-Since"] = served.modified

    response = served.client.get("/file", headers=headers)

    assert (response.status_code, response.content) == (200, BODY)


@pytest.mark.parametrize(
    ("since", "status"),
    [("same", 304), ("later", 304), ("earlier", 200), ("garbage", 200)],
)
def test_if_modified_since_answers_304_when_the_file_is_not_newer(served: Served, since: str, status: int):
    mtime = served.path.stat().st_mtime
    dates = {
        "same": served.modified,
        "later": formatdate(mtime + 3600, usegmt=True),
        "earlier": formatdate(mtime - 3600, usegmt=True),
        "garbage": "yesterday-ish",
    }

    response = served.client.get("/file", headers={"If-Modified-Since": dates[since]})

    assert response.status_code == status
    assert response.content == (b"" if status == 304 else BODY)


@pytest.mark.parametrize(
    ("header", "body", "content_range"),
    [
        ("bytes=2-4", b"234", "bytes 2-4/10"),
        ("bytes=7-", b"789", "bytes 7-9/10"),
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=8-99", b"89", "bytes 8-9/10"),
        ("bytes=-99", BODY, "bytes 0-9/10"),
    ],
)
def test_single_byte_range_answers_206_with_its_slice(served: Served, header: str, body: bytes, content_range: str):
    response = served.client.get("/file", headers={"Range": header})

    assert (response.status_code, response.content) == (206, body)
    assert response.headers["content-range"] == content_range
    assert response.headers["content-length"] == str(len(body))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["etag"] == served.etag
    assert response.headers["content-disposition"] == DISPOSITION


@pytest.mark.parametrize(
    ("condition", "status"),
    [
        ("{etag}", 206),
        ("{modified}", 206),
        ("W/{etag}", 200),
        ('"other"', 200),
        ("Mon, 01 Jan 2001 00:00:00 GMT", 200),
    ],
)
def test_if_range_honours_the_range_only_for_the_exact_validator(served: Served, condition: str, status: int):
    settled = time.time() - 10
    os.utime(served.path, (settled, settled))
    headers = {"Range": "bytes=2-4", "If-Range": condition.format(etag=served.etag, modified=served.modified)}

    response = served.client.get("/file", headers=headers)

    assert response.status_code == status
    assert response.content == (b"234" if status == 206 else BODY)


@pytest.mark.parametrize(("condition", "status"), [("{modified}", 200), ("{etag}", 206)])
def test_a_last_modified_that_is_not_yet_a_second_old_cannot_resume_a_range(
    served: Served, condition: str, status: int
):
    # A stamp ahead of the clock keeps the date weak however slowly the request runs.
    ahead = time.time() + 3600
    os.utime(served.path, (ahead, ahead))
    headers = {"Range": "bytes=2-4", "If-Range": condition.format(etag=served.etag, modified=served.modified)}

    response = served.client.get("/file", headers=headers)

    assert response.status_code == status
    assert response.content == (b"234" if status == 206 else BODY)


@pytest.mark.parametrize(
    ("content", "header"),
    [(BODY, "bytes=10-"), (BODY, "bytes=99-100"), (BODY, "bytes=-0"), (b"", "bytes=0-0"), (b"", "bytes=-5")],
)
def test_unsatisfiable_range_answers_416_with_the_size(served: Served, content: bytes, header: str):
    _ = served.path.write_bytes(content)

    response = served.client.get("/file", headers={"Range": header})

    assert (response.status_code, response.content) == (416, b"")
    assert response.headers["content-range"] == f"bytes */{len(content)}"


@pytest.mark.parametrize(
    "header", ["bytes=0-1,3-4", "bytes=abc", "bytes=-", "bytes=5-2", "bytes=1-2-3", "items=0-1", "0-3"]
)
def test_multiple_malformed_or_foreign_ranges_are_ignored(served: Served, header: str):
    response = served.client.get("/file", headers={"Range": header})

    assert (response.status_code, response.content) == (200, BODY)
    assert "content-range" not in response.headers


@pytest.mark.parametrize(
    ("headers", "status"),
    [({"if-none-match": "*"}, 304), ({"range": "bytes=10-"}, 416)],
)
def test_an_answer_without_a_body_closes_the_stream_before_returning(
    tmp_path: Path, headers: dict[str, str], status: int
):
    target = tmp_path / "report.txt"
    _ = target.write_bytes(BODY)
    stream, info = open_file(target)

    response = file_response(Headers(headers), stream, info, media_type="text/plain", headers={})

    assert response.status_code == status
    assert stream.closed


async def test_a_stream_whose_body_never_starts_is_closed_by_the_background_task(tmp_path: Path):
    target = tmp_path / "report.txt"
    _ = target.write_bytes(BODY)
    stream, info = open_file(target)
    response = file_response(Headers({}), stream, info, media_type="text/plain", headers={})
    assert response.background is not None
    assert not stream.closed

    await response.background()

    assert stream.closed


@pytest.mark.parametrize(("range_header", "body"), [(None, BODY), ("bytes=2-4", b"234")])
async def test_a_file_that_grew_after_it_was_measured_is_cut_at_the_announced_length(
    tmp_path: Path, range_header: str | None, body: bytes
):
    target = tmp_path / "report.txt"
    _ = target.write_bytes(BODY)
    stream, info = open_file(target)
    with target.open("ab") as writer:
        _ = writer.write(b"appended after the length was announced")
    request_headers = Headers({} if range_header is None else {"range": range_header})

    response = file_response(request_headers, stream, info, media_type="text/plain", headers={})
    assert isinstance(response, StreamingResponse)
    chunks = [chunk async for chunk in response.body_iterator if isinstance(chunk, bytes)]

    assert b"".join(chunks) == body
    assert response.headers["content-length"] == str(len(body))


def test_the_validators_follow_the_modification_time(served: Served):
    first = served.client.get("/file").headers["etag"]
    stamp = datetime(2001, 1, 1, tzinfo=UTC).timestamp()
    os.utime(served.path, (stamp, stamp))

    response = served.client.get("/file")

    assert response.headers["etag"] not in {first, ""}
    assert response.headers["last-modified"] == "Mon, 01 Jan 2001 00:00:00 GMT"

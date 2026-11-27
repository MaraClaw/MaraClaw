from __future__ import annotations

from pathlib import Path

import pytest
from tests.bounded_read_fakes import RecordingStorage

from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.text_head import (
    TRUNCATED_MARKER,
    head_bytes_for_chars,
    read_text_capped,
    read_text_head,
)

KEY = "agent/notes.md"
HEAD = 64


async def test_a_file_shorter_than_the_head_decodes_like_a_whole_read() -> None:
    storage = RecordingStorage({KEY: "caf\u00e9 \u65e5\u672c".encode() + b"\xe2\x82"})

    head = await read_text_head(storage, KEY, HEAD)

    assert head.text == "caf\u00e9 \u65e5\u672c\ufffd"
    assert head.more is False
    assert storage.reads == [(KEY, HEAD, 0)]
    assert storage.whole_reads == []


async def test_a_longer_file_gives_its_first_bytes_and_says_it_goes_on() -> None:
    storage = RecordingStorage({KEY: b"a" * (HEAD * 3)})

    head = await read_text_head(storage, KEY, HEAD)

    assert head.text == "a" * HEAD
    assert head.more is True
    assert storage.reads == [(KEY, HEAD, 0)]


async def test_a_file_that_fills_the_head_exactly_may_continue() -> None:
    head = await read_text_head(RecordingStorage({KEY: b"a" * HEAD}), KEY, HEAD)

    assert head.text == "a" * HEAD
    assert head.more is True


@pytest.mark.parametrize("character", ["\u00e9", "\u20ac", "\U0001f600"])
async def test_a_character_cut_at_the_end_of_the_head_is_dropped_not_replaced(character: str) -> None:
    encoded = character.encode()
    for kept in range(1, len(encoded)):
        data = b"a" * (HEAD - kept) + encoded + b"tail"
        head = await read_text_head(RecordingStorage({KEY: data}), KEY, HEAD)

        assert head.text == "a" * (HEAD - kept)
        assert head.more is True


async def test_invalid_bytes_inside_the_head_are_replaced_as_a_whole_read_does() -> None:
    data = b"ab\xffcd" + b"x" * (HEAD * 2)

    head = await read_text_head(RecordingStorage({KEY: data}), KEY, HEAD)

    assert head.text == "ab\ufffdcd" + "x" * (HEAD - 5)


async def test_an_absent_key_is_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        _ = await read_text_head(RecordingStorage({}), KEY, HEAD)


async def test_the_local_backend_serves_the_head_from_the_start_of_the_file(tmp_path: Path) -> None:
    storage = LocalStorageBackend(str(tmp_path))
    await storage.write_bytes(KEY, b"a" * (HEAD - 1) + "\u20ac".encode() + b"tail")

    head = await read_text_head(storage, KEY, HEAD)

    assert head.text == "a" * (HEAD - 1)
    assert head.more is True


async def test_capped_text_is_stripped_and_unmarked_when_the_file_fits() -> None:
    storage = RecordingStorage({KEY: "\n  hello \u20ac  \n".encode()})

    assert await read_text_capped(storage, KEY, 10) == "hello \u20ac"
    assert storage.reads == [(KEY, head_bytes_for_chars(10), 0)]
    assert storage.whole_reads == []


async def test_capped_text_is_cut_and_marked_when_the_file_is_longer() -> None:
    storage = RecordingStorage({KEY: b"x" * 1000})

    assert await read_text_capped(storage, KEY, 10) == "x" * 10 + TRUNCATED_MARKER
    assert storage.reads == [(KEY, head_bytes_for_chars(10), 0)]


async def test_a_character_cut_inside_the_capped_head_is_dropped_and_the_file_is_still_marked() -> None:
    padding = head_bytes_for_chars(4) - 4
    data = b" " * padding + "\u20ac".encode() * 5

    assert await read_text_capped(RecordingStorage({KEY: data}), KEY, 4) == "\u20ac" + TRUNCATED_MARKER


async def test_a_whitespace_only_head_is_empty_however_long_the_file_is() -> None:
    storage = RecordingStorage({KEY: b" \n" * 500 + b"late text"})

    assert await read_text_capped(storage, KEY, 10) == ""

from __future__ import annotations

import asyncio
import importlib
import pickle
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from threading import Event, Lock
from types import ModuleType, SimpleNamespace

import anyio
import pytest

from app.core.thread_lanes import ThreadLane
from app.services import agent_tools, document_conversion
from app.services.agent_tool_exec import (
    _agent_tool_exec_conversion as conversion,
    _agent_tool_exec_storage as storage,
    document_convert,
    document_reading,
    documents,
    workspace,
)
from app.services.agent_tool_exec.registry import ToolArguments
from app.services.agent_tools import ToolParameters
from app.services.document_conversion import conversion_lane, csv as csv_conversion
from thread_lane_fixtures import forbid_shared_threads


def test_document_handlers_when_extracted_then_import_direct_modules() -> None:
    assert hasattr(conversion, "document_convert")
    assert hasattr(storage, "documents")


async def test_read_document_handler_when_called_then_uses_extracted_storage_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[uuid.UUID, str, int, str | None]] = []
    agent_id = uuid.uuid4()

    async def get_tenant_id(_: uuid.UUID) -> str:
        return "tenant-1"

    async def read_from_storage(received_agent_id: uuid.UUID, path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append((received_agent_id, path, max_chars, tenant_id))
        return "extracted result"

    async def legacy_read(*_: object, **__: object) -> str:
        raise AssertionError("legacy facade must not be called")

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", get_tenant_id)
    monkeypatch.setattr(storage.documents, "_read_document_from_storage", read_from_storage)
    monkeypatch.setattr(agent_tools, "_read_document_from_storage", legacy_read)

    handler_result = storage.read_document(
        arguments={"path": "report.pdf", "max_chars": 90000},
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session",
        on_output=None,
    )
    assert not isinstance(handler_result, str)
    result = await handler_result

    assert result == "extracted result"
    assert calls == [(agent_id, "report.pdf", 20000, "tenant-1")]


@pytest.mark.parametrize(
    ("handler_name", "adapter_name"),
    [
        ("convert_csv_to_xlsx", "_convert_csv_to_xlsx"),
        ("convert_html_to_pdf", "_convert_html_to_pdf"),
        ("convert_html_to_pptx", "_convert_html_to_pptx"),
        ("convert_markdown_to_docx", "_convert_markdown_to_docx"),
        ("convert_markdown_to_pdf", "_convert_markdown_to_pdf"),
    ],
)
async def test_conversion_handler_when_called_then_uses_extracted_adapter(
    monkeypatch: pytest.MonkeyPatch, handler_name: str, adapter_name: str
) -> None:
    captured: dict[str, object] = {}
    agent_id = uuid.uuid4()

    async def get_tenant_id(_: uuid.UUID) -> str:
        return "tenant-2"

    async def adapter(_: uuid.UUID, workspace: Path, arguments: ToolArguments) -> str:
        captured["adapter"] = (workspace, arguments)
        return "converted"

    async def legacy_adapter(*_: object, **__: object) -> str:
        raise AssertionError("legacy facade must not be called")

    async def run_workspace(
        received_agent_id: uuid.UUID,
        tenant_id: str,
        runner: Callable[[Path], Awaitable[str]],
        *,
        paths: list[str] | None,
        sync_back: bool,
    ) -> str:
        captured["workspace"] = (received_agent_id, tenant_id, paths, sync_back)
        return await runner(Path("/fake/workspace"))

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", get_tenant_id)
    monkeypatch.setattr(workspace, "_run_with_temp_workspace", run_workspace)
    monkeypatch.setattr(agent_tools, "_run_with_temp_workspace", legacy_adapter)
    monkeypatch.setattr(agent_tools, adapter_name, legacy_adapter)
    monkeypatch.setattr(conversion.document_convert, adapter_name, adapter)

    handler_result = getattr(conversion, handler_name)(
        arguments={"source_path": "input.md", "target_path": "output.pdf"},
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session",
        on_output=None,
    )
    assert not isinstance(handler_result, str)
    result = await handler_result

    assert result == "converted"
    assert captured["workspace"] == (agent_id, "tenant-2", ["input.md", "output.pdf"], True)
    assert captured["adapter"] == (Path("/fake/workspace"), {"source_path": "input.md", "target_path": "output.pdf"})


def test_document_reader_when_text_exceeds_limit_then_clamps_and_truncates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("abcdef", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)

    result = document_reading._read_document_sync(tmp_path, "notes.txt", max_chars=3)

    assert result == "abc\n\n...[truncated, 6 chars total]"


def test_document_reader_when_unsupported_then_returns_legacy_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "notes.bin"
    source.write_bytes(b"data")
    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)

    assert document_reading._read_document_sync(tmp_path, "notes.bin") == (
        "Unsupported file format: .bin. Supported: PDF, DOCX, XLSX, PPTX, TXT, MD, CSV"
    )


def test_document_workers_when_spawned_then_are_module_pickleable_and_report_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents.document_reading, "_read_document_sync", lambda *_args, **_kwargs: "content")

    out_queue = documents.mp.Queue()
    documents._read_document_worker(out_queue, "/workspace", "doc.txt", 8, None)

    assert pickle.dumps(documents._read_document_worker)
    assert out_queue.get(timeout=1) == ("ok", "content")


def test_csv_core_when_delimited_rows_then_detects_delimiter_and_strips_blank_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.csv"
    target = tmp_path / "target.xlsx"
    source.write_text("one;two;;\nthree;four; ;\n", encoding="utf-8")
    rows: list[list[str]] = []
    saved: list[str] = []

    class Worksheet:
        def append(self, values: list[str]) -> None:
            rows.append(values)

    class Workbook:
        def __init__(self) -> None:
            self.active = Worksheet()
            self.worksheets = [self.active]

        def save(self, path: str) -> None:
            saved.append(path)

    real_import_module = importlib.import_module

    def import_module(name: str) -> object:
        if name == "openpyxl":
            return SimpleNamespace(Workbook=Workbook)
        return real_import_module(name)

    monkeypatch.setitem(sys.modules, "openpyxl", import_module("openpyxl"))

    result = csv_conversion._convert_csv_to_xlsx(source, target, "target.xlsx")

    assert result == "✅ Successfully converted CSV to Excel: target.xlsx"
    assert rows == [["one", "two"], ["three", "four"]]
    assert saved == [str(target)]


async def test_html_adapter_when_called_then_delegates_to_legacy_html_public_function(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.html"
    source.write_text("<h1>Test</h1>", encoding="utf-8")
    target = tmp_path / "output.pdf"
    calls: list[tuple[Path, Path, str, Path, ToolArguments]] = []

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(agent_tools, "_resolve_tool_target_path", lambda *_args, **_kwargs: target)

    async def convert_html(src_file: Path, tgt_file: Path, target_path: str, ws: Path, arguments: ToolArguments) -> str:
        calls.append((src_file, tgt_file, target_path, ws, arguments))
        return "html result"

    monkeypatch.setattr(document_conversion, "convert_html_to_pdf", convert_html)

    result = await document_convert._convert_html_to_pdf(
        uuid.uuid4(), tmp_path, {"source_path": "source.html", "target_path": "output.pdf"}
    )

    assert result == "html result"
    assert calls == [
        (source, target, "output.pdf", tmp_path, {"source_path": "source.html", "target_path": "output.pdf"})
    ]


async def test_markdown_adapter_when_core_fails_then_returns_legacy_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Test", encoding="utf-8")
    target = tmp_path / "output.docx"

    class Logger:
        def exception(self, _: str) -> None:
            return None

    def fail(*_: object) -> str:
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(agent_tools, "_resolve_tool_target_path", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(document_convert, "logger", Logger(), raising=False)
    monkeypatch.setattr(document_convert.markdown_conversion, "_convert_markdown_to_docx", fail)

    result = await document_convert._convert_markdown_to_docx(
        uuid.uuid4(), tmp_path, {"source_path": "source.md", "target_path": "output.docx"}
    )

    assert result == "❌ Conversion failed: renderer unavailable"


def test_reader_facades_when_called_then_forward_exact_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    out_queue = documents.mp.Queue()

    def read_sync(ws: Path, rel_path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append(("read_sync", (ws, rel_path), {"max_chars": max_chars, "tenant_id": tenant_id}))
        return "read result"

    def read_worker(queue_value: object, ws_str: str, rel_path: str, max_chars: int, tenant_id: str | None) -> None:
        calls.append(("read_worker", (queue_value, ws_str, rel_path, max_chars, tenant_id), {}))

    def read_timeout(ws: Path, rel_path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append(("read_timeout", (ws, rel_path), {"max_chars": max_chars, "tenant_id": tenant_id}))
        return "read timeout result"

    monkeypatch.setattr(document_reading, "_read_document_sync", read_sync)
    monkeypatch.setattr(documents, "_read_document_worker", read_worker)
    monkeypatch.setattr(documents, "_read_document_with_timeout", read_timeout)

    assert agent_tools._read_document_sync(tmp_path, "input.txt", max_chars=9, tenant_id="tenant") == "read result"
    assert agent_tools._read_document_worker(out_queue, "/workspace", "input.txt", 9, "tenant") is None
    assert (
        agent_tools._read_document_with_timeout(tmp_path, "input.txt", max_chars=9, tenant_id="tenant")
        == "read timeout result"
    )

    assert calls == [
        ("read_sync", (tmp_path, "input.txt"), {"max_chars": 9, "tenant_id": "tenant"}),
        ("read_worker", (out_queue, "/workspace", "input.txt", 9, "tenant"), {}),
        ("read_timeout", (tmp_path, "input.txt"), {"max_chars": 9, "tenant_id": "tenant"}),
    ]


async def test_async_facades_when_called_then_forward_exact_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    agent_id = uuid.uuid4()
    arguments: ToolParameters = {"source_path": "source.md", "target_path": "target.pdf"}

    async def read_document(ws: Path, rel_path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append(("read", (ws, rel_path, max_chars, tenant_id)))
        return "read result"

    async def read_from_storage(
        received_agent_id: uuid.UUID, rel_path: str, max_chars: int, tenant_id: str | None
    ) -> str:
        calls.append(("storage", (received_agent_id, rel_path, max_chars, tenant_id)))
        return "storage result"

    async def csv_convert(received_agent_id: uuid.UUID, ws: Path, received_arguments: ToolArguments) -> str:
        calls.append(("csv", (received_agent_id, ws, received_arguments)))
        return "csv result"

    async def html_pdf(received_agent_id: uuid.UUID, ws: Path, received_arguments: ToolArguments) -> str:
        calls.append(("html_pdf", (received_agent_id, ws, received_arguments)))
        return "html pdf result"

    async def html_pptx(received_agent_id: uuid.UUID, ws: Path, received_arguments: ToolArguments) -> str:
        calls.append(("html_pptx", (received_agent_id, ws, received_arguments)))
        return "html pptx result"

    async def markdown_docx(received_agent_id: uuid.UUID, ws: Path, received_arguments: ToolArguments) -> str:
        calls.append(("markdown_docx", (received_agent_id, ws, received_arguments)))
        return "markdown docx result"

    async def markdown_pdf(received_agent_id: uuid.UUID, ws: Path, received_arguments: ToolArguments) -> str:
        calls.append(("markdown_pdf", (received_agent_id, ws, received_arguments)))
        return "markdown pdf result"

    monkeypatch.setattr(documents, "_read_document", read_document)
    monkeypatch.setattr(documents, "_read_document_from_storage", read_from_storage)
    monkeypatch.setattr(document_convert, "_convert_csv_to_xlsx", csv_convert)
    monkeypatch.setattr(document_convert, "_convert_html_to_pdf", html_pdf)
    monkeypatch.setattr(document_convert, "_convert_html_to_pptx", html_pptx)
    monkeypatch.setattr(document_convert, "_convert_markdown_to_docx", markdown_docx)
    monkeypatch.setattr(document_convert, "_convert_markdown_to_pdf", markdown_pdf)

    assert await agent_tools._read_document(tmp_path, "input.txt", max_chars=9, tenant_id="tenant") == "read result"
    assert (
        await agent_tools._read_document_from_storage(agent_id, "input.txt", max_chars=9, tenant_id="tenant")
        == "storage result"
    )
    assert await agent_tools._convert_csv_to_xlsx(agent_id, tmp_path, arguments) == "csv result"
    assert await agent_tools._convert_html_to_pdf(agent_id, tmp_path, arguments) == "html pdf result"
    assert await agent_tools._convert_html_to_pptx(agent_id, tmp_path, arguments) == "html pptx result"
    assert await agent_tools._convert_markdown_to_docx(agent_id, tmp_path, arguments) == "markdown docx result"
    assert await agent_tools._convert_markdown_to_pdf(agent_id, tmp_path, arguments) == "markdown pdf result"

    assert calls == [
        ("read", (tmp_path, "input.txt", 9, "tenant")),
        ("storage", (agent_id, "input.txt", 9, "tenant")),
        ("csv", (agent_id, tmp_path, arguments)),
        ("html_pdf", (agent_id, tmp_path, arguments)),
        ("html_pptx", (agent_id, tmp_path, arguments)),
        ("markdown_docx", (agent_id, tmp_path, arguments)),
        ("markdown_pdf", (agent_id, tmp_path, arguments)),
    ]


async def test_html_pptx_adapter_when_called_then_preserves_legacy_argument_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.html"
    target = tmp_path / "output.pptx"
    source.write_text("<h1>Test</h1>", encoding="utf-8")
    calls: list[tuple[Path, Path, str, Path, ToolArguments]] = []
    arguments: ToolArguments = {"source_path": "source.html", "target_path": "output.pptx"}

    async def convert_html(
        src_file: Path, tgt_file: Path, target_path: str, ws: Path, received_arguments: ToolArguments
    ) -> str:
        calls.append((src_file, tgt_file, target_path, ws, received_arguments))
        return "pptx result"

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(agent_tools, "_resolve_tool_target_path", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(document_conversion, "convert_html_to_pptx", convert_html)

    assert await document_convert._convert_html_to_pptx(uuid.uuid4(), tmp_path, arguments) == "pptx result"
    assert calls == [(source, target, "output.pptx", tmp_path, arguments)]


async def test_markdown_pdf_adapter_when_core_succeeds_or_fails_then_preserves_legacy_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.md"
    target = tmp_path / "output.pdf"
    source.write_text("# Test", encoding="utf-8")
    arguments: ToolArguments = {"source_path": "source.md", "target_path": "output.pdf"}
    calls: list[tuple[Path, Path, str, Path]] = []

    class Logger:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def exception(self, message: str) -> None:
            self.messages.append(message)

    logger = Logger()

    def succeed(src_file: Path, tgt_file: Path, target_path: str, ws: Path) -> str:
        calls.append((src_file, tgt_file, target_path, ws))
        return "markdown pdf result"

    def fail(*_: object) -> str:
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(agent_tools, "_resolve_tool_target_path", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(document_convert, "logger", logger, raising=False)
    monkeypatch.setattr(document_convert.markdown_conversion, "_convert_markdown_to_pdf", succeed)

    assert await document_convert._convert_markdown_to_pdf(uuid.uuid4(), tmp_path, arguments) == "markdown pdf result"
    assert calls == [(source, target, "output.pdf", tmp_path)]

    monkeypatch.setattr(document_convert.markdown_conversion, "_convert_markdown_to_pdf", fail)
    assert await document_convert._convert_markdown_to_pdf(uuid.uuid4(), tmp_path, arguments) == (
        "❌ Conversion failed: renderer unavailable"
    )
    assert logger.messages == ["Convert MD to PDF failed: renderer unavailable"]


def test_document_reader_when_file_exceeds_size_limit_then_rejects_before_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "oversized.pdf"
    source.write_bytes(b"placeholder")

    class OversizedPath:
        suffix = ".pdf"
        name = "oversized.pdf"

        def exists(self) -> bool:
            return True

        def is_dir(self) -> bool:
            return False

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=51 * 1024 * 1024)

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: OversizedPath())

    assert document_reading._read_document_sync(tmp_path, "oversized.pdf") == (
        "Document is too large to read safely (51.0 MB). "
        "Please split or convert it to a smaller text/Markdown excerpt first."
    )


class Probe:
    """Blocking conversion work that records how many calls run at once and in what order they start and end."""

    def __init__(self) -> None:
        self.entered: asyncio.Queue[str] = asyncio.Queue()
        self.gate: Event = Event()
        self.log: list[str] = []
        self.threads: set[str] = set()
        self.running: int = 0
        self.peak: int = 0
        self._loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self._lock: Lock = Lock()

    def work(self, _src_file: Path, _tgt_file: Path, target_path: str) -> str:
        with self._lock:
            self.log.append(f"start {target_path}")
            self.threads.add(threading.current_thread().name)
            self.running += 1
            self.peak = max(self.peak, self.running)
        self._loop.call_soon_threadsafe(self.entered.put_nowait, target_path)
        assert self.gate.wait(10)
        with self._lock:
            self.running -= 1
            self.log.append(f"end {target_path}")
        return f"converted {target_path}"

    async def wait_entered(self, count: int) -> None:
        for _ in range(count):
            _ = await asyncio.wait_for(self.entered.get(), 10)


class ManualDeadline:
    """The conversion deadline's timers, fired by the test on cue instead of by the clock."""

    def __init__(self) -> None:
        self.timers: list[asyncio.Timeout] = []

    def fire(self, index: int = 0) -> None:
        self.timers[index].reschedule(asyncio.get_running_loop().time())


@pytest.fixture
def deadline(monkeypatch: pytest.MonkeyPatch) -> ManualDeadline:
    manual = ManualDeadline()
    make_timer = asyncio.timeout

    def timeout(_delay: float | None) -> asyncio.Timeout:
        timer = make_timer(None)
        manual.timers.append(timer)
        return timer

    monkeypatch.setattr(asyncio, "timeout", timeout)
    return manual


@pytest.fixture
def convert_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Coroutine[object, object, str]]:
    source = tmp_path / "source.csv"
    _ = source.write_text("a,b\n", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(agent_tools, "_resolve_tool_target_path", lambda *_args, **_kwargs: tmp_path / "out.xlsx")

    def convert(target_path: str) -> Coroutine[object, object, str]:
        return document_convert._convert_csv_to_xlsx(
            uuid.uuid4(), tmp_path, {"source_path": "source.csv", "target_path": target_path}
        )

    return convert


async def test_slow_conversion_runs_on_the_conversion_lane_and_leaves_the_event_loop_free(
    convert_csv: Callable[[str], Coroutine[object, object, str]], monkeypatch: pytest.MonkeyPatch
):
    # Given: a conversion that stays inside its worker thread until released
    probe = Probe()
    monkeypatch.setattr(document_convert, "_csv_to_xlsx", probe.work)
    with forbid_shared_threads(monkeypatch):
        conversion_task = asyncio.create_task(convert_csv("slow.xlsx"))
        await probe.wait_entered(1)

        # When: the event loop is used meanwhile
        ticks = 0
        for _ in range(100):
            await asyncio.sleep(0)
            ticks += 1

        # Then: it kept its schedule, the conversion was still running, and it ran on the conversion lane
        assert ticks == 100
        assert not conversion_task.done()
        probe.gate.set()
        assert await asyncio.wait_for(conversion_task, 10) == "converted slow.xlsx"
    assert {name.rsplit("_", 1)[0] for name in probe.threads} == {"conversion-lane"}


@pytest.mark.parametrize(
    ("handler_name", "owner_name", "core_name"),
    [
        ("_convert_csv_to_xlsx", "document_convert", "_csv_to_xlsx"),
        ("_convert_markdown_to_docx", "markdown_conversion", "_convert_markdown_to_docx"),
        ("_convert_markdown_to_pdf", "markdown_conversion", "_convert_markdown_to_pdf"),
    ],
)
async def test_blocking_conversion_cores_never_run_on_the_event_loop_thread_or_shared_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler_name: str, owner_name: str, core_name: str
):
    # Given: a source file and a core that reports the thread it ran on
    source = tmp_path / "source.md"
    _ = source.write_text("# Title", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(agent_tools, "_resolve_tool_target_path", lambda *_args, **_kwargs: tmp_path / "out")
    owners: dict[str, ModuleType] = {
        "document_convert": document_convert,
        "markdown_conversion": document_convert.markdown_conversion,
    }
    ran_on: list[str] = []

    def core(*_args: object) -> str:
        ran_on.append(threading.current_thread().name)
        return "converted"

    monkeypatch.setattr(owners[owner_name], core_name, core)

    # When: the handler runs without access to the default executor
    with forbid_shared_threads(monkeypatch):
        result = await getattr(document_convert, handler_name)(
            uuid.uuid4(), tmp_path, {"source_path": "source.md", "target_path": "out"}
        )

    # Then: the core ran on a conversion lane thread
    assert result == "converted"
    assert [name.rsplit("_", 1)[0] for name in ran_on] == ["conversion-lane"]


async def test_conversion_lane_runs_at_most_its_configured_size_and_queues_the_rest(
    convert_csv: Callable[[str], Coroutine[object, object, str]], monkeypatch: pytest.MonkeyPatch
):
    # Given: a lane of two threads and five conversions that all block
    probe = Probe()
    monkeypatch.setattr(document_convert, "_csv_to_xlsx", probe.work)
    monkeypatch.setattr(conversion_lane, "CONVERSION_LANE", ThreadLane(2, name="test-conversion-lane"))
    calls = [asyncio.create_task(convert_csv(f"{index}.xlsx")) for index in range(5)]
    try:
        # When: two have started
        await probe.wait_entered(2)
        for _ in range(5):
            await asyncio.sleep(0)

        # Then: the other three wait for a slot
        assert probe.running == 2
        assert len(probe.log) == 2
    finally:
        probe.gate.set()

    # And: every conversion finishes without the lane ever running more than two
    results = await asyncio.wait_for(asyncio.gather(*calls), 10)
    assert results == [f"converted {index}.xlsx" for index in range(5)]
    assert probe.peak == 2


async def test_timed_out_conversion_returns_an_error_and_keeps_its_slot_until_the_thread_ends(
    convert_csv: Callable[[str], Coroutine[object, object, str]],
    deadline: ManualDeadline,
    monkeypatch: pytest.MonkeyPatch,
):
    # Given: a one-slot lane whose only thread is inside a conversion that outlives its deadline
    probe = Probe()
    monkeypatch.setattr(document_convert, "_csv_to_xlsx", probe.work)
    monkeypatch.setattr(conversion_lane, "CONVERSION_LANE", ThreadLane(1, name="test-conversion-lane"))
    slow = asyncio.create_task(convert_csv("slow.xlsx"))
    await probe.wait_entered(1)

    # When: the deadline passes
    deadline.fire()
    result = await asyncio.wait_for(slow, 10)

    # Then: the tool gets an error while the abandoned thread still runs
    assert result.startswith("❌ Conversion failed:")
    assert probe.running == 1
    # And: the next conversion cannot start on the occupied slot, so the lane never runs two
    follower = asyncio.create_task(convert_csv("next.xlsx"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert probe.log == ["start slow.xlsx"]
    # And: it starts only after the abandoned thread has ended
    probe.gate.set()
    assert await asyncio.wait_for(follower, 10) == "converted next.xlsx"
    assert probe.log == ["start slow.xlsx", "end slow.xlsx", "start next.xlsx", "end next.xlsx"]
    assert probe.peak == 1


async def test_html_conversion_past_its_deadline_is_cancelled_and_reports_an_error(
    tmp_path: Path, deadline: ManualDeadline, monkeypatch: pytest.MonkeyPatch
):
    # Given: an HTML render that never finishes
    source = tmp_path / "source.html"
    _ = source.write_text("<p>slow</p>", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(agent_tools, "_resolve_tool_target_path", lambda *_args, **_kwargs: tmp_path / "out.pdf")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def render(*_args: object) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    monkeypatch.setattr(document_conversion, "convert_html_to_pdf", render)
    conversion_task = asyncio.create_task(
        document_convert._convert_html_to_pdf(
            uuid.uuid4(), tmp_path, {"source_path": "source.html", "target_path": "out.pdf"}
        )
    )
    _ = await asyncio.wait_for(started.wait(), 10)

    # When: the deadline passes
    deadline.fire()
    result = await asyncio.wait_for(conversion_task, 10)

    # Then: the render was cancelled and the tool got an error instead of waiting
    assert cancelled.is_set()
    assert result.startswith("❌ Conversion failed:")


async def test_a_cancel_scope_does_not_keep_the_caller_waiting_for_a_running_conversion(tmp_path: Path):
    # Given: a conversion running inside a cancel scope, the kind a request or chat stream is cancelled through
    probe = Probe()
    scopes: list[anyio.CancelScope] = []

    async def convert() -> bool:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            _ = await conversion_lane.run_conversion(probe.work, tmp_path, tmp_path, "scoped.xlsx")
        return scope.cancelled_caught

    caller = asyncio.create_task(convert())
    await probe.wait_entered(1)

    # When: the scope is cancelled while the conversion's thread is still blocked
    scopes[0].cancel()

    # Then: the caller leaves at once and the thread keeps running
    assert await asyncio.wait_for(caller, 10)
    assert probe.running == 1
    probe.gate.set()

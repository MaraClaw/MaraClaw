from __future__ import annotations

import importlib
import pickle
import queue
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from app.services.document_conversion import csv as csv_conversion


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
    assert document_reading._safe_document_cell_text(1 << 4097) == "[large integer omitted]"
    assert document_reading._safe_document_cell_text("x" * 501).endswith("...[cell truncated]")


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


def test_document_timeout_when_worker_exits_without_queue_result_then_returns_lifecycle_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class EmptyQueue:
        def get_nowait(self) -> tuple[str, str]:
            raise queue.Empty

    class FinishedProcess:
        exitcode = 7

        def start(self) -> None:
            return None

        def join(self, _: int) -> None:
            return None

        def is_alive(self) -> bool:
            return False

    def make_queue(*, maxsize: int) -> EmptyQueue:
        assert maxsize == 1
        return EmptyQueue()

    def make_process(**_: object) -> FinishedProcess:
        return FinishedProcess()

    class FakeContext:
        Queue = staticmethod(make_queue)
        Process = staticmethod(make_process)

    monkeypatch.setattr(documents.mp, "get_context", lambda _: FakeContext())

    assert (
        documents._read_document_with_timeout(tmp_path, "doc.txt")
        == "Document read failed: extractor exited with code 7"
    )


def test_pdf_timeout_when_primary_hangs_then_uses_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class HangingProcess:
        exitcode = None

        def start(self) -> None:
            return None

        def join(self, _: int) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    def make_queue(*, maxsize: int) -> SimpleNamespace:
        assert maxsize == 1
        return SimpleNamespace()

    def make_process(**_: object) -> HangingProcess:
        return HangingProcess()

    class FakeContext:
        Queue = staticmethod(make_queue)
        Process = staticmethod(make_process)

    monkeypatch.setattr(documents.mp, "get_context", lambda _: FakeContext())
    monkeypatch.setattr(documents, "_read_pdf_fast_with_timeout", lambda *_args, **_kwargs: "fallback result")

    assert documents._read_document_with_timeout(tmp_path, "doc.pdf") == "fallback result"


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
    calls: list[tuple[Path, Path, str, ToolArguments]] = []

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(agent_tools, "_resolve_tool_target_path", lambda *_args, **_kwargs: target)

    async def convert_html(src_file: Path, tgt_file: Path, target_path: str, arguments: ToolArguments) -> str:
        calls.append((src_file, tgt_file, target_path, arguments))
        return "html result"

    monkeypatch.setattr(document_conversion, "convert_html_to_pdf", convert_html)

    result = await document_convert._convert_html_to_pdf(
        uuid.uuid4(), tmp_path, {"source_path": "source.html", "target_path": "output.pdf"}
    )

    assert result == "html result"
    assert calls == [(source, target, "output.pdf", {"source_path": "source.html", "target_path": "output.pdf"})]


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

    def safe_cell_text(value: object) -> str:
        calls.append(("safe", (value,), {}))
        return "safe result"

    def read_sync(ws: Path, rel_path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append(("read_sync", (ws, rel_path), {"max_chars": max_chars, "tenant_id": tenant_id}))
        return "read result"

    def read_worker(queue_value: object, ws_str: str, rel_path: str, max_chars: int, tenant_id: str | None) -> None:
        calls.append(("read_worker", (queue_value, ws_str, rel_path, max_chars, tenant_id), {}))

    def pdf_sync(ws: Path, rel_path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append(("pdf_sync", (ws, rel_path), {"max_chars": max_chars, "tenant_id": tenant_id}))
        return "pdf result"

    def pdf_worker(queue_value: object, ws_str: str, rel_path: str, max_chars: int, tenant_id: str | None) -> None:
        calls.append(("pdf_worker", (queue_value, ws_str, rel_path, max_chars, tenant_id), {}))

    def pdf_timeout(ws: Path, rel_path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append(("pdf_timeout", (ws, rel_path), {"max_chars": max_chars, "tenant_id": tenant_id}))
        return "timeout result"

    def read_timeout(ws: Path, rel_path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append(("read_timeout", (ws, rel_path), {"max_chars": max_chars, "tenant_id": tenant_id}))
        return "read timeout result"

    monkeypatch.setattr(document_reading, "_safe_document_cell_text", safe_cell_text)
    monkeypatch.setattr(document_reading, "_read_document_sync", read_sync)
    monkeypatch.setattr(documents, "_read_document_worker", read_worker)
    monkeypatch.setattr(documents, "_read_pdf_fast_sync", pdf_sync)
    monkeypatch.setattr(documents, "_read_pdf_fast_worker", pdf_worker)
    monkeypatch.setattr(documents, "_read_pdf_fast_with_timeout", pdf_timeout)
    monkeypatch.setattr(documents, "_read_document_with_timeout", read_timeout)

    assert agent_tools._safe_document_cell_text("value") == "safe result"
    assert agent_tools._read_document_sync(tmp_path, "input.txt", max_chars=9, tenant_id="tenant") == "read result"
    assert agent_tools._read_document_worker(out_queue, "/workspace", "input.txt", 9, "tenant") is None
    assert agent_tools._read_pdf_fast_sync(tmp_path, "input.pdf", max_chars=9, tenant_id="tenant") == "pdf result"
    assert agent_tools._read_pdf_fast_worker(out_queue, "/workspace", "input.pdf", 9, "tenant") is None
    assert (
        agent_tools._read_pdf_fast_with_timeout(tmp_path, "input.pdf", max_chars=9, tenant_id="tenant")
        == "timeout result"
    )
    assert (
        agent_tools._read_document_with_timeout(tmp_path, "input.txt", max_chars=9, tenant_id="tenant")
        == "read timeout result"
    )

    assert calls == [
        ("safe", ("value",), {}),
        ("read_sync", (tmp_path, "input.txt"), {"max_chars": 9, "tenant_id": "tenant"}),
        ("read_worker", (out_queue, "/workspace", "input.txt", 9, "tenant"), {}),
        ("pdf_sync", (tmp_path, "input.pdf"), {"max_chars": 9, "tenant_id": "tenant"}),
        ("pdf_worker", (out_queue, "/workspace", "input.pdf", 9, "tenant"), {}),
        ("pdf_timeout", (tmp_path, "input.pdf"), {"max_chars": 9, "tenant_id": "tenant"}),
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


def test_pdf_fast_worker_when_extractor_succeeds_or_raises_then_reports_queue_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_value = documents.mp.Queue()
    monkeypatch.setattr(documents, "_read_pdf_fast_sync", lambda *_args, **_kwargs: "fast content")

    documents._read_pdf_fast_worker(queue_value, "/workspace", "input.pdf", 9, "tenant")

    assert queue_value.get(timeout=1) == ("ok", "fast content")

    def fail(*_: object, **__: object) -> str:
        raise RuntimeError("fitz unavailable")

    monkeypatch.setattr(documents, "_read_pdf_fast_sync", fail)
    documents._read_pdf_fast_worker(queue_value, "/workspace", "input.pdf", 9, "tenant")

    assert queue_value.get(timeout=1) == ("error", "PDF fallback extraction failed: fitz unavailable")


def test_pdf_reader_when_parser_returns_pages_then_clamps_to_requested_characters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "input.pdf"
    source.write_bytes(b"placeholder")

    class Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class PDF:
        def __init__(self) -> None:
            self.pages = [Page("first"), Page("second")]

        def __enter__(self) -> PDF:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(document_reading.importlib, "import_module", lambda _: SimpleNamespace(open=lambda _: PDF()))

    assert document_reading._read_document_sync(tmp_path, "input.pdf", max_chars=12) == (
        "--- Page 1 -\n\n...[truncated, 20 chars total]"
    )


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

        def exists(self) -> bool:
            return True

        def is_dir(self) -> bool:
            return False

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=51 * 1024 * 1024)

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: OversizedPath())
    monkeypatch.setattr(document_reading.importlib, "import_module", lambda _: (_ for _ in ()).throw(AssertionError()))

    assert document_reading._read_document_sync(tmp_path, "oversized.pdf") == (
        "Document is too large to read safely (51.0 MB). "
        "Please split or convert it to a smaller text/Markdown excerpt first."
    )


def test_pdf_reader_when_more_than_fifty_pages_then_omits_remaining_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "pages.pdf"
    source.write_bytes(b"placeholder")

    class Page:
        def __init__(self, number: int) -> None:
            self.number = number

        def extract_text(self) -> str:
            return f"page {self.number}"

    class PDF:
        def __init__(self) -> None:
            self.pages = [Page(number) for number in range(1, 52)]

        def __enter__(self) -> PDF:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(document_reading.importlib, "import_module", lambda _: SimpleNamespace(open=lambda _: PDF()))

    result = document_reading._read_document_sync(tmp_path, "pages.pdf", max_chars=20000)

    assert "--- Page 50 ---\npage 50" in result
    assert "Page 51" not in result


def test_docx_reader_when_table_has_more_than_column_limit_then_omits_extra_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "table.docx"
    source.write_bytes(b"placeholder")

    class Cell:
        def __init__(self, text: str) -> None:
            self.text = text

    class Row:
        def __init__(self) -> None:
            self.cells = [Cell(f"cell {index}") for index in range(81)]

    document = SimpleNamespace(
        paragraphs=[SimpleNamespace(text="Paragraph")],
        tables=[SimpleNamespace(rows=[Row()])],
        element=SimpleNamespace(body=SimpleNamespace(iter=lambda _: [])),
        sections=[],
    )
    modules = {
        "docx": SimpleNamespace(Document=lambda _: document),
        "docx.oxml.ns": SimpleNamespace(qn=lambda value: value),
    }
    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(document_reading.importlib, "import_module", modules.__getitem__)

    result = document_reading._read_document_sync(tmp_path, "table.docx", max_chars=20000)

    assert result.startswith("Paragraph\ncell 0")
    assert "cell 79" in result
    assert "cell 80" not in result


def test_xlsx_reader_when_cell_limit_reached_then_reports_omitted_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "sheet.xlsx"
    source.write_bytes(b"placeholder")

    class Sheet:
        def iter_rows(self, **_: int) -> list[tuple[str, ...]]:
            return [tuple("" for _ in range(80)) for _ in range(251)]

    class Workbook:
        def __init__(self) -> None:
            self.sheetnames = ["Data"]

        def __getitem__(self, _: str) -> Sheet:
            return Sheet()

        def close(self) -> None:
            return None

    def load_workbook(_: str, *, read_only: bool, data_only: bool) -> Workbook:
        assert read_only is True
        assert data_only is True
        return Workbook()

    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(
        document_reading.importlib, "import_module", lambda _: SimpleNamespace(load_workbook=load_workbook)
    )

    result = document_reading._read_document_sync(tmp_path, "sheet.xlsx", max_chars=20000)

    assert result.startswith("=== Sheet: Data ===")
    assert "[cell limit reached; remaining cells omitted]" in result


def test_pptx_reader_when_more_than_fifty_slides_then_omits_remaining_slides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "slides.pptx"
    source.write_bytes(b"placeholder")
    slides = [SimpleNamespace(shapes=[SimpleNamespace(text=f"slide {number}")]) for number in range(1, 52)]
    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(
        document_reading.importlib,
        "import_module",
        lambda _: SimpleNamespace(Presentation=lambda _: SimpleNamespace(slides=slides)),
    )

    result = document_reading._read_document_sync(tmp_path, "slides.pptx", max_chars=20000)

    assert "--- Slide 50 ---\nslide 50" in result
    assert "Slide 51" not in result


def test_document_reader_when_parser_import_or_read_fails_then_returns_legacy_error_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(agent_tools, "_resolve_tool_source_path", lambda *_args, **_kwargs: source)

    def missing_parser(_: str) -> object:
        raise ImportError("pdfplumber unavailable")

    monkeypatch.setattr(document_reading.importlib, "import_module", missing_parser)
    missing = document_reading._read_document_sync(tmp_path, "broken.pdf")

    def broken_parser(_: str) -> object:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(document_reading.importlib, "import_module", broken_parser)
    broken = document_reading._read_document_sync(tmp_path, "broken.pdf")

    assert (
        missing
        == "Missing dependency: pdfplumber unavailable. Install: pip install pdfplumber python-docx openpyxl python-pptx"
    )
    assert broken == "Document read failed: parser exploded"

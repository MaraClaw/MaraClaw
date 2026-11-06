import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.records.gateway_message import GatewayMessageRecord
from app.services import turn_timing
from app.services.turn_timing import AdapterTiming, RelayTimeline, TurnTimeline

CREATED = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)


@pytest.fixture
def lines(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str | int]]]:
    captured: list[tuple[str, dict[str, str | int]]] = []

    def info(template: str, payload: str) -> None:
        captured.append((template, json.loads(payload)))

    monkeypatch.setattr(turn_timing, "logger", SimpleNamespace(info=info))
    return captured


def at(milliseconds: int) -> datetime:
    return CREATED + timedelta(milliseconds=milliseconds)


def test_turn_offsets_and_adapter_durations_keep_separate_clock_origins(lines) -> None:
    # Given a claimed row whose database claim time is 120 ms after submission.
    message = GatewayMessageRecord(uuid4(), uuid4(), "private prompt", created_at=CREATED, delivered_at=at(120))
    timeline = TurnTimeline.claimed(message)
    timeline.mark("exec_started", at(150))
    timeline.mark("first_delta", at(1300))
    timeline.mark("rpc_returned", at(1500))
    # When the adapter reports offsets from its own process start, and later steps are marked.
    timeline.adapter(AdapterTiming(hello_ok=10, accepted=40, terminal=900))
    timeline.mark("settled", at(1600))
    timeline.mark("published", at(1605))
    timeline.emit("terminal")
    # Then engine marks use created_at while adapter durations retain their independent origin.
    assert lines == [
        (
            "[TurnTiming] {}",
            {
                "message_id": str(message.id),
                "agent_id": str(message.agent_id),
                "outcome": "terminal",
                "claimed": 120,
                "exec_started": 150,
                "first_delta": 1300,
                "rpc_returned": 1500,
                "adapter_hello_ok": 10,
                "adapter_accepted": 40,
                "adapter_terminal": 900,
                "settled": 1600,
                "published": 1605,
            },
        )
    ]


def test_emit_logs_exactly_once(lines) -> None:
    timeline = TurnTimeline(uuid4(), uuid4(), CREATED)
    timeline.mark("claimed", at(5))
    timeline.emit("not_admitted")
    timeline.emit("failed")
    assert [payload["outcome"] for _, payload in lines] == ["not_admitted"]


def test_adapter_missing_steps_are_not_invented_and_need_no_engine_clock_anchor(lines) -> None:
    timeline = TurnTimeline(uuid4(), uuid4(), CREATED)
    timeline.mark("claimed", at(5))
    timeline.adapter(AdapterTiming(hello_ok=7))
    timeline.adapter(None)
    timeline.emit("uncertain")
    ((_, payload),) = lines
    assert {k: v for k, v in payload.items() if k not in {"message_id", "agent_id"}} == {
        "outcome": "uncertain",
        "claimed": 5,
        "adapter_hello_ok": 7,
    }


def test_logging_failure_never_reaches_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("log sink down")

    monkeypatch.setattr(turn_timing, "logger", SimpleNamespace(info=broken))
    timeline = TurnTimeline(uuid4(), uuid4(), CREATED)
    timeline.mark("claimed", at(1))
    timeline.emit("terminal")
    relay = RelayTimeline(uuid4(), uuid4(), "im", 1, completed_at=CREATED, result="acknowledged")
    relay.emit()


def test_relay_line_reports_result_and_offsets_from_completed_at(lines) -> None:
    timeline = RelayTimeline(uuid4(), uuid4(), "im", 2, completed_at=datetime.now(UTC) - timedelta(seconds=3))
    timeline.emit()
    assert lines == []
    timeline.result = "chunk_sent"
    timeline.mark("claimed")
    timeline.mark("sent")
    timeline.emit()
    ((template, payload),) = lines
    assert template == "[RelayTiming] {}"
    assert payload["result"] == "chunk_sent"
    assert (payload["destination_kind"], payload["attempt"]) == ("im", 2)
    assert set(payload) == {
        "message_id",
        "delivery_id",
        "destination_kind",
        "attempt",
        "result",
        "claimed",
        "sent",
        "done",
    }
    assert 3000 <= payload["claimed"] <= payload["sent"] <= payload["done"]

import io

from scripts import latency_report

LOG = """\
2026-11-06 09:00:01 | INFO | trace | app.services.turn_timing:88 - [TurnTiming] {"message_id":"a","agent_id":"x","outcome":"terminal","claimed":10,"exec_started":20,"first_delta":300,"rpc_returned":1080,"adapter_hello_ok":30,"adapter_accepted":60,"adapter_terminal":1000,"settled":1100,"published":1105}
unrelated line with [TurnTiming] but not json
2026-11-06 09:00:02 | INFO | trace | app.services.turn_timing:88 - [TurnTiming] {"message_id":"b","agent_id":"x","outcome":"terminal","claimed":30,"exec_started":40,"first_delta":400,"rpc_returned":2100,"adapter_accepted":60,"adapter_terminal":2000,"settled":2110,"published":2112}
2026-11-06 09:00:03 | INFO | trace | app.services.turn_timing:88 - [TurnTiming] {"message_id":"c","agent_id":"x","outcome":"not_admitted","claimed":50}
2026-11-06 09:00:04 | INFO | trace | app.services.turn_timing:117 - [RelayTiming] {"message_id":"a","delivery_id":"d","destination_kind":"im","attempt":1,"result":"acknowledged","claimed":12,"sent":200,"done":210}
2026-11-06 09:00:05 | INFO | trace | app.services.turn_timing:117 - [RelayTiming] {"message_id":"b","delivery_id":"e","destination_kind":"agent","attempt":1,"result":"acknowledged","claimed":40,"done":90}
"""


def test_parse_keeps_only_integer_marks_of_each_kind() -> None:
    turns, relays = latency_report.parse_lines(LOG.splitlines())
    assert len(turns) == 3
    assert turns[0] == {
        "claimed": 10,
        "exec_started": 20,
        "first_delta": 300,
        "rpc_returned": 1080,
        "adapter_hello_ok": 30,
        "adapter_accepted": 60,
        "adapter_terminal": 1000,
        "settled": 1100,
        "published": 1105,
    }
    assert turns[2] == {"claimed": 50}
    assert relays == [{"claimed": 12, "sent": 200, "done": 210}, {"claimed": 40, "done": 90}]


def test_percentiles_use_nearest_rank() -> None:
    ordered = list(range(1, 101))
    assert [latency_report.percentile(ordered, q) for q in (50, 95, 99)] == [50, 95, 99]
    assert latency_report.percentile([7], 99) == 7


def test_report_prints_percentiles_for_offsets_and_derived_intervals() -> None:
    turn_section = latency_report.report(LOG.splitlines()).split("\n\n")[0]
    # Columns after the metric name: count, p50, p95, p99.
    rows = {line.split()[0]: line.split()[1:5] for line in turn_section.splitlines()[2:]}
    assert rows["claimed"] == ["3", "30", "50", "50"]
    assert rows["claimed->exec_started"] == ["2", "10", "10", "10"]
    assert rows["exec_started->first_delta"] == ["2", "280", "360", "360"]
    assert rows["adapter_accepted->adapter_terminal"] == ["2", "940", "1940", "1940"]
    assert rows["rpc_returned->settled"] == ["2", "10", "20", "20"]
    assert rows["settled->published"] == ["2", "2", "5", "5"]


def test_report_without_matching_lines_says_so() -> None:
    assert latency_report.report(["nothing here"]) == "No [TurnTiming] or [RelayTiming] lines found."


def test_main_reads_files_or_stdin(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "engine.log"
    path.write_text(LOG, encoding="utf-8")
    assert latency_report.main([str(path)]) == 0
    from_file = capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(LOG))
    assert latency_report.main([]) == 0
    assert capsys.readouterr().out == from_file
    assert "TurnTiming: 3 lines" in from_file
    assert "RelayTiming: 2 lines" in from_file

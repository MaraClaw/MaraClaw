"""Prometheus metrics: text exposition, request and pool series, and the token-guarded scrape route."""

import hmac
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api import metrics as metrics_api
from app.api.metrics import build_metrics_router
from app.config import Settings, get_settings
from app.core import metrics
from app.db import pool as pool_module

TOKEN = "s3cret-token"


class StatsPool:
    def __init__(self, stats: dict[str, int]) -> None:
        self._stats: dict[str, int] = stats

    def get_stats(self) -> dict[str, int]:
        return dict(self._stats)


@pytest.fixture(autouse=True)
def _no_pools() -> None:
    pool_module.reset_pool_for_tests()


def test_histogram_renders_cumulative_buckets_sum_and_count_per_label_set() -> None:
    # Given observations on two label sets, one exactly on a bucket bound.
    histogram = metrics.Histogram("demo_seconds", "Demo.", ("route",), (0.125, 1.0))
    for value in (0.0625, 0.5, 4.0):
        histogram.observe(value, "/a")
    histogram.observe(0.125, "/b")

    # When rendered.
    text = metrics.render([histogram.collect()])

    # Then buckets are cumulative with a +Inf overflow, the bound is inclusive, and each label set has sum and count.
    assert text == (
        "# HELP demo_seconds Demo.\n"
        "# TYPE demo_seconds histogram\n"
        'demo_seconds_bucket{route="/a",le="0.125"} 1\n'
        'demo_seconds_bucket{route="/a",le="1"} 2\n'
        'demo_seconds_bucket{route="/a",le="+Inf"} 3\n'
        'demo_seconds_sum{route="/a"} 4.5625\n'
        'demo_seconds_count{route="/a"} 3\n'
        'demo_seconds_bucket{route="/b",le="0.125"} 1\n'
        'demo_seconds_bucket{route="/b",le="1"} 1\n'
        'demo_seconds_bucket{route="/b",le="+Inf"} 1\n'
        'demo_seconds_sum{route="/b"} 0.125\n'
        'demo_seconds_count{route="/b"} 1\n'
    )


def test_a_histogram_without_observations_renders_only_its_header() -> None:
    assert metrics.render([metrics.Histogram("idle_seconds", "Idle.", (), (1.0,)).collect()]) == (
        "# HELP idle_seconds Idle.\n# TYPE idle_seconds histogram\n"
    )


def test_label_values_and_help_text_are_escaped() -> None:
    histogram = metrics.Histogram("esc_seconds", "Back\\slash and\nnewline.", ("route",), (1.0,))
    histogram.observe(0.5, 'a"b\\c\nd')

    lines = metrics.render([histogram.collect()]).splitlines()

    assert lines[0] == "# HELP esc_seconds Back\\\\slash and\\nnewline."
    assert 'esc_seconds_count{route="a\\"b\\\\c\\nd"} 1' in lines


def test_sample_values_use_prometheus_number_syntax() -> None:
    family = metrics.Family(
        "numbers",
        "gauge",
        "Numbers.",
        tuple(
            metrics.Sample("numbers", (("case", name),), value)
            for name, value in (
                ("integer", 3.0),
                ("fraction", 0.25),
                ("small", 2.5e-05),
                ("infinite", math.inf),
                ("not_a_number", math.nan),
            )
        ),
    )

    assert metrics.render([family]).splitlines()[2:] == [
        'numbers{case="integer"} 3',
        'numbers{case="fraction"} 0.25',
        'numbers{case="small"} 2.5e-05',
        'numbers{case="infinite"} +Inf',
        'numbers{case="not_a_number"} NaN',
    ]


def test_unknown_request_methods_share_one_label(monkeypatch: pytest.MonkeyPatch) -> None:
    histogram = metrics.Histogram("latency", "Latency.", ("method", "route", "status"), (1.0,))
    monkeypatch.setattr(metrics, "HTTP_REQUEST_SECONDS", histogram)

    for method in ("GET", "BREW", "PROPFIND"):
        metrics.observe_http_request(method, "unmatched-route", 404, 0.5)

    counts = {
        dict(sample.labels)["method"]: sample.value
        for sample in histogram.collect().samples
        if sample.name == "latency_count"
    }
    assert counts == {"GET": 1, "OTHER": 2}


def test_a_scrape_reports_both_pools_and_the_log_drop_total(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a saturated request pool, an idle durable pool that has counted nothing yet, and seven dropped records.
    main = StatsPool(
        {
            "pool_min": 2,
            "pool_max": 30,
            "pool_size": 30,
            "pool_available": 0,
            "requests_waiting": 12,
            "requests_num": 900,
            "requests_queued": 40,
            "requests_wait_ms": 2500,
            "requests_errors": 3,
        }
    )
    durable = StatsPool({"pool_min": 0, "pool_max": 4, "pool_size": 1, "pool_available": 1, "requests_waiting": 0})
    monkeypatch.setattr(pool_module, "_pool", main)
    monkeypatch.setattr(pool_module, "_durable", (main, durable))
    monkeypatch.setattr(metrics, "get_logging_service", lambda: SimpleNamespace(dropped_total=7))

    # When scraped.
    lines = set(metrics.render_metrics().splitlines())

    # Then each pool reports its size, free connections, queue and counters, and the drop total is a counter.
    assert {
        'maraclaw_db_pool_max_connections{pool="main"} 30',
        'maraclaw_db_pool_connections{pool="main"} 30',
        'maraclaw_db_pool_available_connections{pool="main"} 0',
        'maraclaw_db_pool_waiting_requests{pool="main"} 12',
        'maraclaw_db_pool_requests_total{pool="main"} 900',
        'maraclaw_db_pool_requests_queued_total{pool="main"} 40',
        'maraclaw_db_pool_request_errors_total{pool="main"} 3',
        'maraclaw_db_pool_request_wait_seconds_total{pool="main"} 2.5',
        'maraclaw_db_pool_available_connections{pool="durable"} 1',
        'maraclaw_db_pool_waiting_requests{pool="durable"} 0',
        'maraclaw_db_pool_requests_total{pool="durable"} 0',
        "# TYPE maraclaw_db_pool_waiting_requests gauge",
        "# TYPE maraclaw_db_pool_requests_queued_total counter",
        "maraclaw_log_records_dropped_total 7",
        "# TYPE maraclaw_log_records_dropped_total counter",
        "# TYPE maraclaw_http_request_duration_seconds histogram",
        "# TYPE maraclaw_event_loop_lag_seconds histogram",
    } <= lines


def test_a_scrape_before_the_pool_opens_has_no_pool_series() -> None:
    text = metrics.render_metrics()

    assert "# TYPE maraclaw_db_pool_waiting_requests gauge" in text
    assert not [line for line in text.splitlines() if line.startswith("maraclaw_db_pool_") and "{" in line]


def _app(token: str = TOKEN) -> FastAPI:
    app = FastAPI()
    app.include_router(build_metrics_router(token))
    return app


async def _scrape(app: FastAPI, authorization: str | bytes | None = None) -> httpx.Response:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"Authorization", authorization.encode() if isinstance(authorization, str) else authorization))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/api/metrics", headers=headers)


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer wrong",
        f"Bearer {TOKEN}-extra",
        f"Bearer  {TOKEN}",
        f"Basic {TOKEN}",
        TOKEN,
        b"Bearer caf\xe9",
    ],
)
async def test_the_metrics_route_refuses_a_request_without_the_exact_bearer_token(
    authorization: str | bytes | None,
) -> None:
    response = await _scrape(_app(), authorization)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "maraclaw_" not in response.text


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_the_metrics_route_serves_prometheus_text_to_the_right_token(scheme: str) -> None:
    response = await _scrape(_app(), f"{scheme} {TOKEN}")

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert response.text.endswith("\n")
    assert "# TYPE maraclaw_http_request_duration_seconds histogram" in response.text.splitlines()


async def test_the_token_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    compared: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(presented: bytes, expected: bytes) -> bool:
        compared.append((presented, expected))
        return real(presented, expected)

    monkeypatch.setattr(metrics_api.hmac, "compare_digest", spy)

    await _scrape(_app(), f"Bearer {TOKEN}")
    await _scrape(_app(), "Bearer wrong")

    assert compared == [(TOKEN.encode(), TOKEN.encode()), (b"wrong", TOKEN.encode())]


def test_an_empty_token_never_builds_a_route() -> None:
    with pytest.raises(ValueError, match="non-empty token"):
        build_metrics_router("")


def test_the_route_stays_out_of_the_openapi_schema() -> None:
    assert "/api/metrics" not in _app().openapi()["paths"]


async def test_the_real_app_answers_404_while_no_token_is_configured() -> None:
    from app.main import app

    assert get_settings().METRICS_TOKEN == ""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/metrics", headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 404


def test_the_real_app_mounts_the_route_only_when_a_token_is_configured() -> None:
    probe = (
        "import json\n"
        "from fastapi.routing import iter_route_contexts\n"
        "from app.main import app\n"
        "print(json.dumps('/api/metrics' in {context.path_format for context in iter_route_contexts(app.routes)}))\n"
    )
    engine = Path(__file__).resolve().parents[1]

    def mounted(token: str) -> object:
        completed = subprocess.run(  # noqa: S603 - fixed argv, the interpreter running this test
            [sys.executable, "-c", probe],
            cwd=engine,
            env={**os.environ, "METRICS_TOKEN": token},
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return json.loads(completed.stdout.splitlines()[-1])

    assert (mounted(""), mounted(TOKEN)) == (False, True)


def test_metrics_settings_defaults_and_boundaries() -> None:
    defaults = Settings()
    assert defaults.METRICS_TOKEN == ""
    assert defaults.EVENT_LOOP_LAG_INTERVAL_SECONDS == 1.0
    for accepted in (0.1, 60):
        interval = Settings.model_validate(
            {"EVENT_LOOP_LAG_INTERVAL_SECONDS": accepted}
        ).EVENT_LOOP_LAG_INTERVAL_SECONDS
        assert interval == accepted
    for rejected in (0, 0.09, 60.5):
        with pytest.raises(ValidationError):
            Settings.model_validate({"EVENT_LOOP_LAG_INTERVAL_SECONDS": rejected})

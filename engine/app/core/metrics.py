"""In-process metrics and their Prometheus text exposition (format 0.0.4).

Histograms are plain objects that the event loop thread updates and renders, so observing takes no lock. Everything
else is read when scraped from state that already exists (the pools' own ``get_stats()``, the log writer's drop
count): a scrape checks out no connection and takes no lock a request path uses.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.core.logging import get_logging_service
from app.db.pool import PoolStats, pool_stats

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

type Labels = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Sample:
    name: str
    labels: Labels
    value: float


@dataclass(frozen=True, slots=True)
class Family:
    name: str
    kind: Literal["counter", "gauge", "histogram"]
    help: str
    samples: tuple[Sample, ...]


@dataclass(slots=True)
class _Series:
    """Counts of one label set: one slot per bucket bound plus the overflow slot, and the sum of the observations."""

    counts: list[int]
    total: float = 0.0


class Histogram:
    """Cumulative-bucket histogram keyed by label values; observe and collect on the event loop thread only."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...], buckets: tuple[float, ...]) -> None:
        self.name: str = name
        self.help: str = help_text
        self.label_names: tuple[str, ...] = label_names
        self.buckets: tuple[float, ...] = buckets
        self._series: dict[tuple[str, ...], _Series] = {}

    def observe(self, value: float, *label_values: str) -> None:
        series = self._series.get(label_values)
        if series is None:
            series = self._series[label_values] = _Series([0] * (len(self.buckets) + 1))
        series.counts[bisect_left(self.buckets, value)] += 1
        series.total += value

    def collect(self) -> Family:
        samples: list[Sample] = []
        for label_values in sorted(self._series):
            series = self._series[label_values]
            labels: Labels = tuple(zip(self.label_names, label_values, strict=True))
            cumulative = 0
            for bound, count in zip((*self.buckets, math.inf), series.counts, strict=True):
                cumulative += count
                samples.append(Sample(f"{self.name}_bucket", (*labels, ("le", _number(bound))), cumulative))
            samples.append(Sample(f"{self.name}_sum", labels, series.total))
            samples.append(Sample(f"{self.name}_count", labels, cumulative))
        return Family(self.name, "histogram", self.help, tuple(samples))


# Route latency spans milliseconds to the longest handler; lag is normally sub-millisecond, so its scale starts lower.
_LATENCY_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_LAG_BUCKETS: Final = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
# Any other request method (a scanner can send any token) shares one label so the series count stays bounded.
_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

HTTP_REQUEST_SECONDS: Final = Histogram(
    "maraclaw_http_request_duration_seconds",
    "Seconds from request arrival to response start, by method, route template and status.",
    ("method", "route", "status"),
    _LATENCY_BUCKETS,
)
EVENT_LOOP_LAG_SECONDS: Final = Histogram(
    "maraclaw_event_loop_lag_seconds",
    "Seconds an event-loop timer woke later than scheduled.",
    (),
    _LAG_BUCKETS,
)


def observe_http_request(method: str, route: str, status: int, seconds: float) -> None:
    """Record one request; ``route`` must be a route template (or the fixed unmatched label), never a raw path."""
    HTTP_REQUEST_SECONDS.observe(seconds, method if method in _METHODS else "OTHER", route, str(status))


def observe_event_loop_lag(seconds: float) -> None:
    EVENT_LOOP_LAG_SECONDS.observe(seconds)


def _per_pool(
    name: str,
    kind: Literal["counter", "gauge"],
    help_text: str,
    readings: Sequence[PoolStats],
    value: Callable[[PoolStats], float],
) -> Family:
    return Family(name, kind, help_text, tuple(Sample(name, (("pool", pool.name),), value(pool)) for pool in readings))


def _pool_families() -> tuple[Family, ...]:
    readings = pool_stats()
    return (
        _per_pool(
            "maraclaw_db_pool_max_connections",
            "gauge",
            "Configured maximum size of the pool.",
            readings,
            lambda pool: pool.max_size,
        ),
        _per_pool(
            "maraclaw_db_pool_connections",
            "gauge",
            "Connections the pool holds, in use or idle.",
            readings,
            lambda pool: pool.size,
        ),
        _per_pool(
            "maraclaw_db_pool_available_connections",
            "gauge",
            "Idle connections ready for checkout.",
            readings,
            lambda pool: pool.available,
        ),
        _per_pool(
            "maraclaw_db_pool_waiting_requests",
            "gauge",
            "Requests queued for a connection.",
            readings,
            lambda pool: pool.requests_waiting,
        ),
        _per_pool(
            "maraclaw_db_pool_requests_total",
            "counter",
            "Connection requests made since the pool opened.",
            readings,
            lambda pool: pool.requests,
        ),
        _per_pool(
            "maraclaw_db_pool_requests_queued_total",
            "counter",
            "Connection requests that had to queue since the pool opened.",
            readings,
            lambda pool: pool.requests_queued,
        ),
        _per_pool(
            "maraclaw_db_pool_request_errors_total",
            "counter",
            "Connection requests that failed (wait timeout, full queue or connection error) since the pool opened.",
            readings,
            lambda pool: pool.requests_errors,
        ),
        _per_pool(
            "maraclaw_db_pool_request_wait_seconds_total",
            "counter",
            "Seconds connection requests spent queued since the pool opened.",
            readings,
            lambda pool: pool.requests_wait_ms / 1000,
        ),
    )


def _log_family() -> Family:
    name = "maraclaw_log_records_dropped_total"
    return Family(
        name,
        "counter",
        "Log records rejected because the write queue was full.",
        (Sample(name, (), get_logging_service().dropped_total),),
    )


def _number(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _escape(text: str, *, quote: bool) -> str:
    escaped = text.replace("\\", "\\\\").replace("\n", "\\n")
    return escaped.replace('"', '\\"') if quote else escaped


def _labels(labels: Labels) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{name}="{_escape(value, quote=True)}"' for name, value in labels) + "}"


def render(families: Iterable[Family]) -> str:
    lines: list[str] = []
    for family in families:
        lines.append(f"# HELP {family.name} {_escape(family.help, quote=False)}")
        lines.append(f"# TYPE {family.name} {family.kind}")
        lines.extend(f"{sample.name}{_labels(sample.labels)} {_number(sample.value)}" for sample in family.samples)
    return "\n".join(lines) + "\n"


def render_metrics() -> str:
    """The whole scrape: request latency, event-loop lag, pool saturation and log drops, as Prometheus text."""
    return render((HTTP_REQUEST_SECONDS.collect(), EVENT_LOOP_LAG_SECONDS.collect(), *_pool_families(), _log_family()))

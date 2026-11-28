"""Counter and gauge primitives of ``core/metrics.py``: naming, the shared text format and caller-owned values."""

import pytest

from app.core import metrics, rate_limit, redis_cache
from app.db import session
from app.services import cache_invalidation_worker as worker


def test_a_counter_name_ends_in_total_exactly_once() -> None:
    assert metrics.counter_family("maraclaw_jobs", "Jobs.", ()).name == "maraclaw_jobs_total"
    assert metrics.counter_family("maraclaw_jobs_total", "Jobs.", ()).name == "maraclaw_jobs_total"


def test_additional_families_render_after_the_built_in_ones_in_the_existing_format() -> None:
    calls = metrics.counter_family("maraclaw_probe_calls", "Probe calls.", [((("outcome", "ok"),), 2.0), (((), 1.0))])
    open_now = metrics.gauge_family("maraclaw_probe_open", "Probe open.", [((), 1.0)])

    text = metrics.render_metrics([calls, open_now])

    assert (
        "# HELP maraclaw_probe_calls_total Probe calls.\n# TYPE maraclaw_probe_calls_total counter\n"
        'maraclaw_probe_calls_total{outcome="ok"} 2\nmaraclaw_probe_calls_total 1\n'
    ) in text
    assert "# TYPE maraclaw_probe_open gauge\nmaraclaw_probe_open 1\n" in text
    assert text.index("maraclaw_event_loop_lag_seconds") < text.index("maraclaw_probe_calls_total")


def test_a_scrape_without_additional_families_is_the_scrape_it_always_was() -> None:
    assert metrics.render_metrics() == metrics.render_metrics(())
    assert "maraclaw_probe" not in metrics.render_metrics()


def test_the_domain_families_render_from_plain_state_with_no_redis_pool_or_breaker_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a scrape reached Redis, the pool or the breaker's admission")

    for owner, name in (
        (redis_cache, "_client"),
        (rate_limit, "get_redis"),
        (worker, "get_cache_redis"),
        (session, "get_pool"),
        (redis_cache._breaker, "admit"),
    ):
        monkeypatch.setattr(owner, name, forbidden)

    text = metrics.render_metrics(
        (*redis_cache.metric_families(), *rate_limit.metric_families(), *worker.metric_families())
    )

    for series in (
        "maraclaw_cache_redis_calls_total",
        "maraclaw_cache_redis_breaker_open",
        "maraclaw_rate_limit_fail_open_total",
        "maraclaw_cache_invalidation_settled_total",
        "maraclaw_cache_invalidation_retried_total",
        "maraclaw_cache_invalidation_fenced_total",
        "maraclaw_cache_invalidation_last_batch_size",
    ):
        assert f"# TYPE {series} " in text

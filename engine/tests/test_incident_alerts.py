"""Cooldown claims fail open, release safely on failure, and never retry delivery."""

from dataclasses import replace

import pytest

from app.core.redis_cache import cache_key
from app.runtime import incident_alerts
from app.services.incident_report import IncidentReport, build_report
from incident_alert_fakes import AlertHarness, harness

__all__ = ["harness"]


async def test_shared_dedup_across_workers(harness: AlertHarness) -> None:
    # Given two workers seeing the same static failure fingerprint.
    first, second = incident_alerts.IncidentAlerts(), incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    # When
    await first._process(report)
    await second._process(build_report(RuntimeError(), source="test"))
    # Then a successful shared claim suppresses delivery in the second process.
    assert len(harness.delivered) == 1
    assert len(harness.claims) == 2
    assert harness.claims[0][2:] == (900, True)
    assert harness.releases == []


async def test_local_dedup_when_redis_is_unavailable(harness: AlertHarness) -> None:
    # Given fail-open Redis and an already delivered fingerprint.
    harness.available = False
    alerts = incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    # When
    await alerts._process(report)
    await alerts._process(report)
    # Then
    assert harness.delivered == [report]


async def test_local_ttl_allows_a_later_occurrence(harness: AlertHarness) -> None:
    # Given Redis outage and an expired local claim.
    harness.available = False
    alerts = incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    await alerts._process(report)
    harness.clock += 900
    # When
    await alerts._process(report)
    # Then
    assert harness.delivered == [report, report]


async def test_local_cooldown_storage_is_bounded(harness: AlertHarness) -> None:
    # Given more distinct failures than the local bound, with Redis unavailable.
    harness.available = False
    alerts = incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    # When
    for number in range(257):
        await alerts._process(replace(report, fingerprint=str(number)))
    await alerts._process(replace(report, fingerprint="0"))
    # Then the oldest fingerprint was evicted and storage remains bounded.
    assert len(alerts._cooldowns) == 256
    assert len(harness.delivered) == 258


@pytest.mark.parametrize("outcome", [False, RuntimeError("private SMTP credential")])
async def test_total_failure_releases_claim_without_retry(harness: AlertHarness, outcome: bool | Exception) -> None:
    # Given a failed delivery after a successful shared claim.
    alerts = incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    harness.outcome = outcome
    # When
    await alerts._process(report)
    # Then no retry occurs, and a new occurrence can claim again.
    assert harness.delivered == [report]
    assert harness.values == {}
    assert alerts._cooldowns == {}
    assert harness.releases == [(cache_key("incident", report.fingerprint), str(report.incident_id))]


async def test_new_occurrence_can_deliver_after_total_failure(harness: AlertHarness) -> None:
    # Given the previous attempt failed completely.
    alerts = incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    harness.outcome = False
    await alerts._process(report)
    harness.outcome = True
    # When
    await alerts._process(report)
    # Then
    assert harness.delivered == [report, report]


async def test_release_never_deletes_a_replacement_owner(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the claim expires/is replaced while delivery is failing.
    alerts = incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    key = cache_key("incident", report.fingerprint)

    async def fail(_report: IncidentReport) -> bool:
        harness.values[key] = "replacement-owner"
        return False

    monkeypatch.setattr(harness.delivery, "deliver_incident", fail)
    # When
    await alerts._process(report)
    # Then compare-and-delete preserves the other worker's cooldown.
    assert harness.values == {key: "replacement-owner"}


async def test_already_claimed_and_success_keep_cooldown(harness: AlertHarness) -> None:
    # Given an existing shared claim (including the delivery service's no-recipient success).
    alerts = incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    harness.values[cache_key("incident", report.fingerprint)] = "other-worker"
    # When
    await alerts._process(report)
    await alerts._process(report)
    # Then neither delivery nor ownership release runs.
    assert harness.delivered == []
    assert harness.releases == []
    assert len(harness.claims) == 1


async def test_shared_timeout_falls_back_under_one_second(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a stuck Redis acquisition, bounded across acquisition and SET.
    import asyncio

    async def stuck() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(incident_alerts, "get_cache_redis", stuck)
    alerts = incident_alerts.IncidentAlerts()
    report = build_report(RuntimeError(), source="test")
    # When
    async with asyncio.timeout(0.99):
        await alerts._process(report)
    # Then
    assert harness.delivered == [report]

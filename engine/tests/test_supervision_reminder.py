from datetime import UTC, datetime, timedelta

from app.services.supervision_reminder import _is_reminder_due


def test_reminder_due_returns_false_for_invalid_json_schedule_interval() -> None:
    # Given: a persisted schedule whose interval is not a number
    now = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
    last_reminded_at = now - timedelta(days=2)

    # When: the reminder evaluator receives the malformed schedule
    is_due = _is_reminder_due('{"freq":"daily","interval":"invalid","time":"09:00"}', last_reminded_at, now)

    # Then: malformed persisted configuration does not crash the scheduler
    assert is_due is False

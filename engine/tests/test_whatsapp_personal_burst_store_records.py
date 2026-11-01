from dataclasses import replace
from uuid import uuid4

import pytest

from app.records.personal_burst import (
    BURST_SEPARATOR,
    PendingUsage,
    PersonalBurstLimits,
    PersonalBurstScope,
)
from app.records.personal_burst_input import NewBurstMember, ReceiptKey

LIMITS = PersonalBurstLimits(
    quiet_seconds=2,
    max_seconds=5,
    pending_count=50,
    pending_bytes=65536,
    batch_bytes=16384,
    notice_cooldown_seconds=30,
)


def member(text: str) -> NewBurstMember:
    return NewBurstMember(ReceiptKey(uuid4(), "whatsapp", "native"), text, None)


@pytest.mark.parametrize(
    "override",
    [
        {"quiet_seconds": 0},
        {"max_seconds": -1},
        {"pending_count": 0},
        {"pending_bytes": 0},
        {"batch_bytes": 0},
        {"notice_cooldown_seconds": 0},
        {"quiet_seconds": 6},
        {"batch_bytes": 65537},
    ],
)
def test_limits_reject_non_positive_and_inverted_bounds(override):
    # Given a valid default configuration, When one bound is non-positive or inverted, Then construction fails.
    with pytest.raises(ValueError, match="personal burst"):
        replace(LIMITS, **override)


def test_limits_accept_equal_quiet_and_max_and_equal_batch_and_backlog():
    assert replace(LIMITS, quiet_seconds=5, batch_bytes=65536).quiet_seconds == 5


@pytest.mark.parametrize(
    ("usage", "incoming", "expected"),
    [
        (PendingUsage(0, 0), 4096 * 4, "admit"),
        (PendingUsage(49, 65536 - 10), 10, "admit"),
        (PendingUsage(50, 0), 1, "capacity"),
        (PendingUsage(0, 65536 - 9), 10, "capacity"),
        (PendingUsage(0, 0), 16385, "oversize"),
        (PendingUsage(50, 65536), 16385, "oversize"),
    ],
)
def test_admission_orders_oversize_before_capacity_and_counts_originals(usage, incoming, expected):
    assert usage.admission(LIMITS, incoming) == expected


def test_member_bytes_are_exact_utf8_not_code_points():
    assert member("é").text_bytes == 2
    assert member("😀" * 4096).text_bytes == 16384 == LIMITS.batch_bytes


def test_scope_equality_covers_every_fence():
    scope = PersonalBurstScope(
        agent_id=uuid4(),
        tenant_id=uuid4(),
        shared_session_id=uuid4(),
        reply_session_id=uuid4(),
        sender_user_id=uuid4(),
        config_id=uuid4(),
        account_id="acct",
        auth_epoch=uuid4(),
        generation=uuid4(),
        peer_id="peer",
    )
    params = scope.params()
    assert set(params) == {
        "agent_id",
        "tenant_id",
        "shared_session_id",
        "reply_session_id",
        "sender_user_id",
        "config_id",
        "provider",
        "account_id",
        "auth_epoch",
        "generation",
        "peer_id",
    }
    for field, other in (("generation", uuid4()), ("peer_id", "x"), ("reply_session_id", uuid4())):
        assert replace(scope, **{field: other}) != scope
    assert BURST_SEPARATOR == "\n\n"

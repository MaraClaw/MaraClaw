"""Engine side of the guest's boot-bound ``AgentBootstrapCompleted`` handshake.

The guest image's helper validates the event for the *current* boot (kernel boot id plus PID 1 start ticks) before it
checks ``/readyz`` or execs an action, and exits 75 when the event is missing, stale or malformed. The engine reaches
the gateway only through these argv shapes, so no readiness or work contact precedes the current boot's event.

``exec --`` keeps the action's own exit status, so exit 75 alone does not prove a refusal. A refusal is exit 75 together
with the helper's own stderr line.
"""

from collections.abc import Sequence

BOOTSTRAP_HELPER = "/usr/local/bin/bootstrap-agent-container.py"
BOOTSTRAP_PENDING_EXIT = 75
BOOTSTRAP_PENDING_STDERR = "bootstrap-agent-container: bootstrap pending:"


class GuestBootstrapPendingError(Exception):
    """The helper refused an action (exit 75 plus its stderr line), so the guest never admitted it."""


def check_ready_argv() -> list[str]:
    """Event check, then the gateway readiness check; exit 0 only when both pass."""
    return ["python3", BOOTSTRAP_HELPER, "check-ready"]


def guarded_argv(argv: Sequence[str]) -> list[str]:
    """Run ``argv`` only if the event is current; it is the helper that execs, so there is no check/use gap."""
    return ["python3", BOOTSTRAP_HELPER, "exec", "--", *argv]

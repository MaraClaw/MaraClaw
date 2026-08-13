"""Keep the AgentBay SDK from resetting process logging."""

from __future__ import annotations

import sys


def disable_agentbay_logger_override() -> None:
    """Disable AgentBay SDK's logging override so it cannot reset our sinks."""
    agentbay_logger_module = sys.modules.get("agentbay._common.logger")
    agentbay_logger = getattr(agentbay_logger_module, "AgentBayLogger", None)
    if agentbay_logger is None:
        return
    agentbay_logger._initialized = True
    agentbay_logger.setup = classmethod(lambda cls, *args, **kwargs: None)

"""High-performance process logging.

Application code should import from here:

    from app.core.logging import logger
    from app.core.logging import get_logger, new_trace_id

Do not import ``loguru`` in app modules. This package must not import
``app.config`` - ``app.config`` loads sandbox config, which logs.
"""

from app.core.logging.adapter import Logger, get_logger, logger
from app.core.logging.agentbay import disable_agentbay_logger_override
from app.core.logging.context import get_trace_id, new_trace_id, set_trace_id, trace_id_var
from app.core.logging.intercept import intercept_standard_logging, quiet_noisy_connection_loggers
from app.core.logging.service import (
    LoggingService,
    configure_logging,
    flush_logging,
    get_logging_service,
    shutdown_logging,
)

# Configure from the environment on first import so early module logs work.
configure_logging()

__all__ = [
    "Logger",
    "LoggingService",
    "configure_logging",
    "disable_agentbay_logger_override",
    "flush_logging",
    "get_logger",
    "get_logging_service",
    "get_trace_id",
    "intercept_standard_logging",
    "logger",
    "new_trace_id",
    "quiet_noisy_connection_loggers",
    "set_trace_id",
    "shutdown_logging",
    "trace_id_var",
]

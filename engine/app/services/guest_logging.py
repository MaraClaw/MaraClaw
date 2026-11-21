"""Log bound for newly created guest containers.

Docker's default json-file driver never rotates, so one chatty guest left running can fill the host disk. ``max-size``
is understood only by the json-file and local drivers; under journald, awslogs and the like it makes ``docker run``
fail. Guests never choose a driver, so the effective one is the daemon default: the bound is passed when that is
json-file or local, and never when it is anything else or cannot be read. It applies on creation only. A resumed or
running guest keeps its log configuration and is not recreated to pick the bound up.
"""

from typing import TypedDict

from pywhales import ClientNotFoundError, DockerClient
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException

from app.config import get_settings
from app.core.logging import logger

settings = get_settings()

_SIZE_BOUNDED_DRIVERS = frozenset({"json-file", "local"})


class LogOptionsKeyword(TypedDict, total=False):
    """Docker's ``log_options`` keyword, present only when the daemon's default driver takes size options."""

    log_options: list[str]


def guest_log_options(client: DockerClient) -> LogOptionsKeyword:
    """The keyword that bounds a new guest's logs, or nothing. Blocking (``docker system info``): use a worker thread."""
    try:
        driver = client.system.info().logging_driver
    except (ClientNotFoundError, DockerException, ParsingError) as exc:
        logger.warning("Guest log bound skipped: daemon logging driver unreadable ({})", type(exc).__name__)
        return {}
    if driver not in _SIZE_BOUNDED_DRIVERS:
        return {}
    return {
        "log_options": [
            f"max-size={settings.OPENCLAW_GUEST_LOG_MAX_SIZE}",
            f"max-file={settings.OPENCLAW_GUEST_LOG_MAX_FILE}",
        ]
    }

"""Warn-only startup isolation diagnostics."""

import shutil
import subprocess
from pathlib import Path

from app.config import get_settings
from app.core.logging import logger


def log_bwrap_startup_status() -> None:
    in_container = Path("/.dockerenv").exists()
    bwrap = shutil.which("bwrap")
    if bwrap:
        try:
            stat = Path(bwrap).stat()
            logger.info(
                "[startup] bubblewrap detected at {} ({}); mode={} setuid={} owner_uid={}",
                bwrap,
                "container" if in_container else "host",
                oct(stat.st_mode & 0o7777),
                bool(stat.st_mode & 0o4000),
                stat.st_uid,
            )
        except OSError as exc:
            logger.warning("[startup] bubblewrap stat failed: {}", exc)
        try:
            probe = subprocess.run(  # noqa: S603 - fixed argv, binary from shutil.which
                [
                    bwrap,
                    "--die-with-parent",
                    "--unshare-pid",
                    "--unshare-user-try",
                    "--ro-bind-try",
                    "/usr",
                    "/usr",
                    "--ro-bind-try",
                    "/bin",
                    "/bin",
                    "--ro-bind-try",
                    "/lib",
                    "/lib",
                    "--ro-bind-try",
                    "/lib64",
                    "/lib64",
                    "--",
                    "/bin/true",
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if probe.returncode == 0:
                logger.info("[startup] bubblewrap namespace probe succeeded")
            else:
                logger.warning(
                    "[startup] bubblewrap namespace probe failed (exit {}): {}. Check host userns/setuid configuration.",
                    probe.returncode,
                    probe.stderr.decode("utf-8", errors="replace")[:300],
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("[startup] bubblewrap namespace probe could not run: {}", exc)
    else:
        logger.warning(
            "[startup] bubblewrap is missing on {}; execute_code {}",
            "container" if in_container else "host",
            "uses reduced isolation"
            if get_settings().SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING
            else "fails closed unless SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING=true",
        )

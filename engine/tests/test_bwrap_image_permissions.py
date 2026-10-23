import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("initial_mode", [0o755, 0o4755])
def test_image_permissions_keep_bwrap_executable_without_setuid(tmp_path: Path, initial_mode: int) -> None:
    # Given: an installed binary, including images whose base layer has setuid.
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bwrap.chmod(initial_mode)
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    entrypoint.chmod(0o644)
    source = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    production = source.partition(" AS production\n")[2].replace("\\\n", " ")
    permissions = next(
        line.removeprefix("RUN ")
        for line in production.splitlines()
        if line.startswith("RUN ") and "/app/entrypoint.sh" in line and "bwrap" in line
    ).replace("/app/entrypoint.sh", shlex.quote(str(entrypoint)))

    # When: execute the real RUN command, even with the retired opt-in supplied.
    subprocess.run(
        ["/bin/sh", "-eu"],
        input=permissions,
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin", "BWRAP_SETUID": "1"},
        check=True,
        capture_output=True,
        text=True,
    )

    # Then: bubblewrap stays runnable but cannot enter its unsupported setuid mode.
    assert stat.S_IMODE(bwrap.stat().st_mode) == 0o755
    assert entrypoint.stat().st_mode & stat.S_IXUSR

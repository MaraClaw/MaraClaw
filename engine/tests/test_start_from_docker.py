from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "start-from-docker.sh"


def test_docker_run_uses_narrow_bwrap_caps() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--cap-add=ALL" not in source
    run_flags = {line.strip().rstrip("\\").strip() for line in source.splitlines()}
    assert "--privileged" not in run_flags
    for cap in ("SYS_ADMIN", "SETUID", "SETGID", "SYS_CHROOT", "SETPCAP"):
        assert f"--cap-add={cap}" in source
    assert "--security-opt seccomp=unconfined" in source
    assert "sandbox escape can reach the host" in source

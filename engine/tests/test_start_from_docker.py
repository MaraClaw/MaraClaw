from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "start-from-docker.sh"
ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"


def test_docker_run_uses_narrow_bwrap_caps() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--cap-add=ALL" not in source
    run_flags = {line.strip().rstrip("\\").strip() for line in source.splitlines()}
    assert "--privileged" not in run_flags
    for cap in ("SYS_ADMIN", "SETUID", "SETGID", "SYS_CHROOT", "SETPCAP", "NET_ADMIN", "SYS_PTRACE"):
        assert f"--cap-add={cap}" in source
    assert "--security-opt seccomp=unconfined" in source
    assert "sandbox escape can reach the host" in source


def test_docker_run_exposes_host_docker_for_agent_containers() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'network inspect "$DOCKER_NETWORK"' in source
    assert 'network create "$DOCKER_NETWORK"' in source
    assert '--network "$DOCKER_NETWORK"' in source
    assert "--network-alias maraclaw-engine" in source
    assert "/var/run/docker.sock" in source
    assert '-v "${DOCKER_SOCK}:/var/run/docker.sock"' in source
    assert '-v "${DATA_DIR}:${DATA_DIR}"' in source
    assert '-e "AGENT_DATA_DIR=${AGENT_DATA_DIR}"' in source
    assert '-e "STORAGE_LOCAL_ROOT=${AGENT_DATA_DIR}"' in source
    assert '-e "DOCKER_NETWORK=${DOCKER_NETWORK}"' in source
    assert '[ "$key" = "DOCKER_NETWORK" ] && continue' in source
    assert '[ "$key" = "STORAGE_LOCAL_ROOT" ] && continue' in source
    assert "docker:26-cli" in source
    assert '-v "${DOCKER_CLI_HOST}:/usr/local/bin/docker:ro"' in source
    assert '-v "${SCRIPT_DIR}/entrypoint.sh:/app/entrypoint.sh:ro"' in source


def test_entrypoint_grants_app_user_docker_socket_group() -> None:
    source = ENTRYPOINT.read_text(encoding="utf-8")
    assert "/var/run/docker.sock" in source
    assert "usermod -aG" in source
    assert "gosu maraclaw" in source
    assert "chmod 666" not in source
    assert "do not chmod the host socket" in source

#!/usr/bin/env bash
# Prepare deploy/.env and host paths as described in deploy/README.md before running.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(uname -s)" == Darwin || "$#" -gt 0 ]]; then
    cd "$SCRIPT_DIR"
    exec python3 -m deploy.starter "$@"
fi
[[ "$(uname -s)" == Linux && "$EUID" == 0 ]] || {
    printf '%s\n' 'Run as root on the local Linux Docker host.' >&2; exit 1;
}
for executable in docker python3 systemctl timeout install; do
    command -v "$executable" >/dev/null || { printf 'Missing prerequisite: %s\n' "$executable" >&2; exit 1; }
done
[[ -d /run/systemd/system ]] || { printf '%s\n' 'systemd must be running.' >&2; exit 1; }
timeout 30 python3 -c 'import sys; assert sys.version_info >= (3, 14)'
timeout 30 docker compose version --short >/dev/null
timeout 35 python3 -c 'import subprocess; v=subprocess.run(["docker","compose","version","--short"],capture_output=True,text=True,check=True,timeout=30).stdout.strip().lstrip("v"); assert tuple(map(int,v.split("-")[0].split(".")[:2])) >= (2,24)'
CONTEXT_HOST="$(timeout 30 docker context inspect --format '{{.Endpoints.docker.Host}}')"
[[ "${DOCKER_HOST:-$CONTEXT_HOST}" == unix:///var/run/docker.sock ]] || { printf '%s\n' 'The rootful local /var/run/docker.sock daemon is required.' >&2; exit 1; }
export DOCKER_HOST=unix:///var/run/docker.sock
unset DOCKER_CONTEXT
[[ "$(timeout 30 docker info --format '{{.OSType}}')" == linux ]] || exit 1
[[ "$SCRIPT_DIR" =~ ^[a-zA-Z0-9_./-]+$ ]] || { printf '%s\n' 'Repository path must not contain systemd metacharacters.' >&2; exit 1; }
COMPOSE=(env -i "PATH=$PATH" "HOME=$HOME" "DOCKER_HOST=$DOCKER_HOST" docker compose
    --project-directory "$SCRIPT_DIR"
    --env-file "$SCRIPT_DIR/deploy/.env"
    -f "$SCRIPT_DIR/compose.yaml")

timeout 30 "${COMPOSE[@]}" config --quiet
cd "$SCRIPT_DIR"
STATE_DIR="$(timeout 30 "${COMPOSE[@]}" config --format json --no-env-resolution | timeout 35 python3 -c 'import json,sys; print(next(v["source"] for v in json.load(sys.stdin)["services"]["ingress"]["volumes"] if v["target"]=="/run/maraclaw-autoscaler"))')"
[[ "$STATE_DIR" == /* && ! -L "$STATE_DIR" ]] || exit 1
timeout 260 systemctl stop maraclaw-autoscaler.service 2>/dev/null || {
    timeout 30 systemctl list-unit-files maraclaw-autoscaler.service --no-legend | timeout 35 python3 -c 'import sys; assert not sys.stdin.read().strip()'
}
timeout 1800 "${COMPOSE[@]}" build
SCALES="$(timeout 180 python3 -m deploy.autoscale --check-startup)"
timeout 30 install -d -o root -g root -m 0700 "$STATE_DIR"
timeout 30 python3 - "$SCRIPT_DIR" "$(command -v python3)" <<'PY'
from pathlib import Path
import sys
root, python = sys.argv[1:]
text = (Path(root) / "deploy/maraclaw-autoscaler.service").read_text()
Path("/etc/systemd/system/maraclaw-autoscaler.service").write_text(text.replace("@ROOT@", root).replace("@PYTHON@", python))
PY
timeout 30 systemctl daemon-reload
read -r -a SCALE_ARGS <<< "$SCALES"
timeout 1800 "${COMPOSE[@]}" up -d --no-build "${SCALE_ARGS[@]}"
timeout 180 python3 -m deploy.autoscale --record-startup
timeout 30 systemctl enable --now maraclaw-autoscaler.service

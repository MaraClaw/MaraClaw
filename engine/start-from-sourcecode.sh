#!/usr/bin/env bash
# Start the MaraClaw backend directly from source.
#
# Requirements on the host:
#   - Python >= 3.14.5 (see pyproject.toml)
#   - System libraries (Debian/Ubuntu names; install the equivalent on macOS via Homebrew):
#       libpq-dev libcairo2 libpango-1.0-0 libpangocairo-1.0-0
#       libgdk-pixbuf-2.0-0 shared-mime-info pango1.0-tools
#   - Either `uv` (recommended) OR plain `python` + `pip`
#
# Usage:
#   ./start-from-sourcecode.sh                       # foreground, port 8000
#   PORT=9000 ./start-from-sourcecode.sh             # override port
#   RELOAD=1 ./start-from-sourcecode.sh              # enable uvicorn --reload
#   SKIP_MIGRATIONS=1 ./start-from-sourcecode.sh     # skip schema bootstrap
#   SKIP_INSTALL=1 ./start-from-sourcecode.sh        # skip dependency install
#
# Any extra args are forwarded to uvicorn.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
APP_TARGET="${APP_TARGET:-app.main:app}"
VENV_DIR="${VENV_DIR:-.venv}"

# Load .env if present (export every var defined in it).
if [ -f .env ]; then
    echo "[run] Loading .env"
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# ---- Pick a runner: uv if available, otherwise venv + pip ------------------
if command -v uv >/dev/null 2>&1; then
    RUNNER="uv"
    echo "[run] Using uv"
else
    RUNNER="venv"
    echo "[run] uv not found, falling back to python venv + pip"
fi

# ---- Install dependencies --------------------------------------------------
if [ "${SKIP_INSTALL:-0}" != "1" ]; then
    if [ "$RUNNER" = "uv" ]; then
        echo "[run] Syncing dependencies via uv (locked)"
        uv sync --locked
        EXEC=(uv run --)
    else
        if [ ! -d "$VENV_DIR" ]; then
            echo "[run] Creating virtualenv at $VENV_DIR"
            python3 -m venv "$VENV_DIR"
        fi
        # shellcheck disable=SC1091
        . "$VENV_DIR/bin/activate"
        echo "[run] Installing project into $VENV_DIR"
        pip install --upgrade pip
        pip install -e .
        EXEC=()
    fi
else
    echo "[run] SKIP_INSTALL=1, not installing dependencies"
    if [ "$RUNNER" = "uv" ]; then
        EXEC=(uv run --)
    else
        # Activate venv if it exists; otherwise rely on system python.
        if [ -d "$VENV_DIR" ]; then
            # shellcheck disable=SC1091
            . "$VENV_DIR/bin/activate"
        fi
        EXEC=()
    fi
fi

# ---- Schema bootstrap ------------------------------------------------------
if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
    echo "[run] Running schema bootstrap (python -m app.scripts.bootstrap_db)"
    "${EXEC[@]}" python -m app.scripts.bootstrap_db
else
    echo "[run] SKIP_MIGRATIONS=1, skipping schema bootstrap"
fi

# ---- Start uvicorn ---------------------------------------------------------
UVICORN_ARGS=("$APP_TARGET" --host "$HOST" --port "$PORT")
if [ "${RELOAD:-0}" = "1" ]; then
    UVICORN_ARGS+=(--reload)
fi
# Forward any extra CLI args.
UVICORN_ARGS+=("$@")

echo "[run] Starting: uvicorn ${UVICORN_ARGS[*]}"
exec "${EXEC[@]}" uvicorn "${UVICORN_ARGS[@]}"

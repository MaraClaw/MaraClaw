#!/bin/bash
# Docker entrypoint: optionally bootstrap schema, then start the app.

set -e

PROCESS_ROLE="${PROCESS_ROLE:-all}"
ALLOW_MIGRATION_FAILURE="${ALLOW_MIGRATION_FAILURE:-false}"
START_COMMAND="${START_COMMAND:-uvicorn app.main:app --host 0.0.0.0 --port 8000}"

role_contains() {
    case ",${PROCESS_ROLE}," in
        *,all,*|*,"$1",*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Permission fixing and privilege dropping ---
if [ "$(id -u)" = '0' ]; then
    echo "[entrypoint] Detected root user, checking permissions..."
    TARGET_DIR="${AGENT_DATA_DIR:-/data/agents}"
    mkdir -p "${TARGET_DIR}"
    CURRENT_OWNER=$(stat -c '%U:%G' "${TARGET_DIR}" 2>/dev/null || echo "")
    if [ "${CURRENT_OWNER}" != "maraclaw:maraclaw" ]; then
        echo "[entrypoint] Directory ${TARGET_DIR} owner is '${CURRENT_OWNER}', fixing permissions..."
        chown -R maraclaw:maraclaw "${TARGET_DIR}"
    else
        echo "[entrypoint] Directory ${TARGET_DIR} is already owned by maraclaw:maraclaw, skipping chown."
    fi

    echo "[entrypoint] Dropping privileges to 'maraclaw' and re-executing..."
    exec gosu maraclaw /bin/bash "$0" "$@"
fi
# -------------------------------------------------------

if [ -z "${INSTANCE_ID:-}" ]; then
    SAFE_PROCESS_ROLE="${PROCESS_ROLE//,/-}"
    _rand=$(cut -c1-8 /proc/sys/kernel/random/uuid 2>/dev/null || printf '%04x%04x' $RANDOM $RANDOM)
    INSTANCE_ID="${SAFE_PROCESS_ROLE}-$(hostname)-${_rand}"
    unset _rand
    export INSTANCE_ID
fi
echo "[entrypoint] INSTANCE_ID=${INSTANCE_ID}"

if role_contains "bootstrap"; then
    echo "[entrypoint] Step 1: Bootstrapping schema for PROCESS_ROLE=${PROCESS_ROLE}..."
    set +e
    BOOT_OUTPUT=$(python -m app.scripts.bootstrap_db 2>&1)
    BOOT_EXIT=$?
    set -e

    if [ $BOOT_EXIT -ne 0 ]; then
        echo ""
        echo "========================================================================"
        echo "[entrypoint] ERROR: Schema bootstrap FAILED (exit code $BOOT_EXIT)"
        echo "========================================================================"
        echo ""
        echo "$BOOT_OUTPUT"
        echo ""
        if [ "$ALLOW_MIGRATION_FAILURE" = "true" ]; then
            echo "[entrypoint] Continuing because ALLOW_MIGRATION_FAILURE=true"
        else
            exit $BOOT_EXIT
        fi
    else
        echo "[entrypoint] Schema bootstrap completed successfully."
    fi
else
    echo "[entrypoint] Step 1: Skipping schema bootstrap for PROCESS_ROLE=${PROCESS_ROLE}"
fi

echo "[entrypoint] Step 2: Starting uvicorn..."
exec sh -c "$START_COMMAND"

#!/usr/bin/env bash
# Startup wrapper: wait for session, then run panel_monitor.py with logging.
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/msi-p13-display"
CONFIG_FILE="${STATE_DIR}/install.conf"
LOG_FILE="${STATE_DIR}/panel-monitor.log"
MAX_WAIT_SECONDS=180

mkdir -p "${STATE_DIR}"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "missing ${CONFIG_FILE}; run ./scripts/install.sh" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "${CONFIG_FILE}"

exec >>"${LOG_FILE}" 2>&1
echo "=== $(date -Is) panel monitor driver starting (pid $$) ==="
echo "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
echo "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-}"
echo "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}"

wait_for_repo() {
    local waited=0
    while [[ ! -x "${PYTHON}" || ! -f "${SCRIPT}" ]]; do
        if (( waited >= MAX_WAIT_SECONDS )); then
            echo "timed out waiting for repo (${PYTHON}, ${SCRIPT})"
            return 1
        fi
        echo "waiting for repo mount (${waited}s)"
        sleep 2
        waited=$((waited + 2))
    done
}

wait_for_wayland() {
    local waited=0
    while true; do
        if [[ -z "${WAYLAND_DISPLAY:-}" && -n "${XDG_RUNTIME_DIR:-}" ]]; then
            local socket
            socket=$(find "${XDG_RUNTIME_DIR}" -maxdepth 1 -name 'wayland-*' -type s 2>/dev/null | head -1)
            if [[ -n "${socket}" ]]; then
                WAYLAND_DISPLAY="${socket##*/}"
                export WAYLAND_DISPLAY
                echo "detected WAYLAND_DISPLAY=${WAYLAND_DISPLAY}"
            fi
        fi
        if [[ "${XDG_SESSION_TYPE:-}" == "wayland" || -n "${WAYLAND_DISPLAY:-}" ]]; then
            return 0
        fi
        if (( waited >= MAX_WAIT_SECONDS )); then
            echo "timed out waiting for Wayland session"
            return 1
        fi
        echo "waiting for Wayland session (${waited}s)"
        sleep 2
        waited=$((waited + 2))
    done
}

wait_for_kwin() {
    local waited=0
    while ! gdbus introspect --session --dest org.kde.KWin \
        --object-path /org/kde/KWin/ScreenShot2 >/dev/null 2>&1; do
        if (( waited >= MAX_WAIT_SECONDS )); then
            echo "timed out waiting for KWin ScreenShot2"
            return 1
        fi
        echo "waiting for KWin ScreenShot2 (${waited}s)"
        sleep 2
        waited=$((waited + 2))
    done
}

wait_for_repo
wait_for_wayland
wait_for_kwin

echo "launching: ${PYTHON} ${SCRIPT} ${DRIVER_ARGS}"
while true; do
    # shellcheck disable=SC2086
    if "${PYTHON}" "${SCRIPT}" ${DRIVER_ARGS}; then
        echo "panel monitor exited cleanly"
        exit 0
    fi
    echo "panel monitor exited with error; restarting in 5s"
    sleep 5
done

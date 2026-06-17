#!/usr/bin/env bash
# Startup wrapper: wait for Plasma/Wayland, then run panel_monitor.py with logging.
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/msi-p13-display"
CONFIG_FILE="${STATE_DIR}/install.conf"
LOG_FILE="${STATE_DIR}/panel-monitor.log"
MAX_WAIT_SECONDS=300

mkdir -p "${STATE_DIR}"

if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "missing ${CONFIG_FILE}; run ./scripts/install.sh" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "${CONFIG_FILE}"

export REPO_ROOT="${REPO_ROOT:-}"
export PYTHON="${PYTHON:-/usr/bin/python3}"
export PYTHONPATH="${PYTHONPATH:-}"
export SCRIPT="${PANEL_MONITOR_SCRIPT:-}"
export PANEL_MONITOR_MODULE="${PANEL_MONITOR_MODULE:-msi_p13_display.panel_monitor}"
export PANEL_MONITOR_SCRIPT="${PANEL_MONITOR_SCRIPT:-}"

if [[ -z "${PYTHONPATH}" || -z "${PANEL_MONITOR_MODULE}" ]]; then
    echo "install.conf is incomplete; run ./scripts/install.sh" >&2
    exit 1
fi

exec >>"${LOG_FILE}" 2>&1
echo "=== $(date -Is) panel monitor driver starting (pid $$) ==="
echo "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
echo "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-}"
echo "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}"
echo "XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-}"
echo "PYTHON=${PYTHON}"
echo "PANEL_MONITOR_MODULE=${PANEL_MONITOR_MODULE}"
echo "PANEL_MONITOR_SCRIPT=${PANEL_MONITOR_SCRIPT:-}"
echo "PYTHONPATH=${PYTHONPATH}"

ensure_session_bus() {
    if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
        return 0
    fi
    if [[ -n "${XDG_RUNTIME_DIR:-}" && -S "${XDG_RUNTIME_DIR}/bus" ]]; then
        DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
        export DBUS_SESSION_BUS_ADDRESS
        echo "detected DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS}"
        return 0
    fi
    return 1
}

wait_for_repo() {
    local waited=0
    while [[ ! -x "${PYTHON}" ]] || [[ -n "${PANEL_MONITOR_SCRIPT:-}" && ! -f "${PANEL_MONITOR_SCRIPT}" ]]; do
        if (( waited >= MAX_WAIT_SECONDS )); then
            echo "timed out waiting for repo (${PYTHON}, ${PANEL_MONITOR_SCRIPT:-${PANEL_MONITOR_MODULE}})"
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

wait_for_session_bus() {
    local waited=0
    while ! ensure_session_bus; do
        if (( waited >= MAX_WAIT_SECONDS )); then
            echo "timed out waiting for session D-Bus"
            return 1
        fi
        echo "waiting for session D-Bus (${waited}s)"
        sleep 2
        waited=$((waited + 2))
    done
}

wait_for_vkms() {
    local waited=0
    while true; do
        if compgen -G "/sys/class/drm/card[0-9]-Virtual-*" >/dev/null; then
            return 0
        fi
        if modprobe vkms 2>/dev/null; then
            sleep 1
            continue
        fi
        if (( waited >= MAX_WAIT_SECONDS )); then
            echo "timed out waiting for vkms DRM module"
            return 1
        fi
        echo "waiting for vkms DRM module (${waited}s)"
        sleep 2
        waited=$((waited + 2))
    done
}

wait_for_kscreen() {
    local waited=0
    while ! kscreen-doctor -j >/dev/null 2>&1; do
        if (( waited >= MAX_WAIT_SECONDS )); then
            echo "timed out waiting for kscreen-doctor"
            return 1
        fi
        echo "waiting for kscreen-doctor (${waited}s)"
        sleep 2
        waited=$((waited + 2))
    done
}

wait_for_repo
wait_for_wayland
wait_for_session_bus
wait_for_vkms
wait_for_kscreen

echo "launching: ${PYTHON} -m ${PANEL_MONITOR_MODULE} ${DRIVER_ARGS}"
while true; do
    # shellcheck disable=SC2086
    if "${PYTHON}" -m "${PANEL_MONITOR_MODULE}" ${DRIVER_ARGS}; then
        echo "panel monitor exited cleanly"
        exit 0
    fi
    echo "panel monitor exited with error; restarting in 5s"
    sleep 5
done

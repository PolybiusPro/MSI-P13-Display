#!/usr/bin/env bash
# Install the MSI P13 panel monitor as a systemd user service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="msi-p13-panel-monitor.service"
DRIVER_ARGS="--quiet --retry-seconds 5"

SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/msi-p13-display"
INSTALL_LIB="${HOME}/.local/lib/msi-p13-display"

SERVICE_TARGET="${SYSTEMD_DIR}/${SERVICE_NAME}"
CONFIG_FILE="${STATE_DIR}/install.conf"
DRIVER_TARGET="${INSTALL_LIB}/panel-monitor-driver.sh"
LOG_FILE="${STATE_DIR}/panel-monitor.log"
LEGACY_AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/org.msi.p13.panel-monitor.desktop"
LEGACY_LAUNCHER="${XDG_DATA_HOME:-$HOME/.local/share}/applications/org.msi.p13.panel-monitor.desktop"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--remove]

Install and enable the MSI P13 panel monitor as a systemd user service.

  systemctl --user status ${SERVICE_NAME}
  journalctl --user -u ${SERVICE_NAME} -f
  tail -f ${LOG_FILE}

Options:
  --remove   disable the service and remove installed files
  -h, --help show this help
EOF
}

resolve_python() {
    if [[ -x "${ROOT}/.venv/bin/python" ]]; then
        echo "${ROOT}/.venv/bin/python"
        return
    fi
    if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
        echo "${VIRTUAL_ENV}/bin/python"
        return
    fi
    command -v python3
}

write_config() {
    mkdir -p "${STATE_DIR}"
    cat >"${CONFIG_FILE}" <<EOF
REPO_ROOT=${ROOT}
PYTHON=${PYTHON}
SCRIPT=${SCRIPT}
DRIVER_ARGS="${DRIVER_ARGS}"
EOF
}

install_driver_wrapper() {
    mkdir -p "${INSTALL_LIB}"
    install -m 755 "${ROOT}/scripts/panel-monitor-driver.sh" "${DRIVER_TARGET}"
    write_config
}

install_systemd_service() {
    mkdir -p "${SYSTEMD_DIR}"
    cat >"${SERVICE_TARGET}" <<EOF
[Unit]
Description=MSI P13 USB display panel driver
After=graphical-session.target plasma-workspace.target
Wants=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=${DRIVER_TARGET}
Restart=always
RestartSec=5
PassEnvironment=WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XDG_CURRENT_DESKTOP HOME USER LOGNAME

[Install]
WantedBy=graphical-session.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "${SERVICE_NAME}"
}

remove_install() {
    if [[ -f "${SERVICE_TARGET}" ]]; then
        systemctl --user disable --now "${SERVICE_NAME}" >/dev/null 2>&1 || true
        rm -f "${SERVICE_TARGET}"
        systemctl --user daemon-reload
    fi
    rm -f "${DRIVER_TARGET}" "${CONFIG_FILE}" "${LEGACY_AUTOSTART}" "${LEGACY_LAUNCHER}"
    rmdir "${INSTALL_LIB}" 2>/dev/null || true
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$(dirname "${LEGACY_LAUNCHER}")" >/dev/null 2>&1 || true
    fi
    echo "Removed panel monitor service and driver wrapper."
    echo "Log kept at: ${LOG_FILE}"
}

REMOVE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remove)
            REMOVE=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

PYTHON="$(resolve_python)"
SCRIPT="${ROOT}/examples/panel_monitor.py"

if [[ "${REMOVE}" -eq 1 ]]; then
    remove_install
    exit 0
fi

if [[ ! -x "${PYTHON}" ]]; then
    echo "Python not found at ${PYTHON}. Run scripts/linux_setup.sh first." >&2
    exit 1
fi

if ! "${PYTHON}" -c "import msi_p13_display" 2>/dev/null; then
    echo "Package msi_p13_display is not installed for ${PYTHON}." >&2
    echo "Run: source .venv/bin/activate && pip install -e ." >&2
    exit 1
fi

install_driver_wrapper
rm -f "${LEGACY_AUTOSTART}" "${LEGACY_LAUNCHER}"
install_systemd_service

echo "Enabled user service: ${SERVICE_NAME}"
echo
echo "Driver wrapper: ${DRIVER_TARGET}"
echo "Config: ${CONFIG_FILE}"
echo "Logs: ${LOG_FILE}"
echo
echo "Status:"
echo "  systemctl --user status ${SERVICE_NAME}"
echo "  journalctl --user -u ${SERVICE_NAME} -f"
echo "  tail -f ${LOG_FILE}"
echo
echo "Remove:"
echo "  $(basename "$0") --remove"

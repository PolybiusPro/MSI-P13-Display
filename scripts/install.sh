#!/usr/bin/env bash
# Install MSI P13 display driver: deps, venv, udev rule, systemd service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

SERVICE_NAME="msi-p13-panel-monitor.service"
DRIVER_ARGS="--quiet --retry-seconds 5"
UDEV_RULE="${ROOT}/scripts/99-msi-p13-display.rules"

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
Usage: $(basename "$0") [options]

Install the MSI P13 USB display driver on Linux.

  - system packages (Python, libusb, krfb, python3-dbus on Fedora)
  - Python venv and editable package install
  - udev rule for non-root USB access
  - systemd user service (starts at graphical login)

Options:
  --remove       disable service and remove installed files (keeps venv)
  --skip-udev    do not install the udev rule
  --skip-service do not install or start the systemd user service
  -h, --help     show this help

Status:
  systemctl --user status ${SERVICE_NAME}
  journalctl --user -u ${SERVICE_NAME} -f
  tail -f ${LOG_FILE}
EOF
}

install_deps_dnf() {
    sudo dnf install -y \
        python3 python3-pip python3-devel libusb-devel \
        krfb python3-dbus python3-gobject
}

install_deps_apt() {
    sudo apt-get update
    sudo apt-get install -y python3-venv python3-dev libusb-1.0-0-dev
    if apt-cache show krfb >/dev/null 2>&1; then
        sudo apt-get install -y krfb python3-dbus python3-gi
    fi
}

install_system_packages() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck source=/dev/null
        source /etc/os-release
    fi

    case "${ID:-}" in
        fedora|rhel|centos)
            install_deps_dnf
            ;;
        debian|ubuntu|raspbian|linuxmint|pop)
            install_deps_apt
            ;;
        *)
            echo "Unsupported distro (${ID:-unknown}). Install python3, python3-venv, libusb dev headers, and krfb manually."
            exit 1
            ;;
    esac
}

install_python_env() {
    python3 -m venv --system-site-packages .venv
    # shellcheck source=/dev/null
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -e .
}

install_udev_rule() {
    if [[ ! -f "${UDEV_RULE}" ]]; then
        echo "warning: udev rule not found at ${UDEV_RULE}" >&2
        return
    fi
    sudo cp "${UDEV_RULE}" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "Installed udev rule. Unplug and replug the display."
}

write_config() {
    mkdir -p "${STATE_DIR}"
    cat >"${CONFIG_FILE}" <<EOF
REPO_ROOT=${ROOT}
PYTHON=${ROOT}/.venv/bin/python
SCRIPT=${ROOT}/examples/panel_monitor.py
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
SKIP_UDEV=0
SKIP_SERVICE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remove)
            REMOVE=1
            ;;
        --skip-udev)
            SKIP_UDEV=1
            ;;
        --skip-service)
            SKIP_SERVICE=1
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

if [[ "${REMOVE}" -eq 1 ]]; then
    remove_install
    exit 0
fi

install_system_packages
install_python_env

if [[ "${SKIP_UDEV}" -eq 0 ]]; then
    install_udev_rule
fi

if [[ "${SKIP_SERVICE}" -eq 0 ]]; then
    install_driver_wrapper
    rm -f "${LEGACY_AUTOSTART}" "${LEGACY_LAUNCHER}"
    install_systemd_service
fi

echo
echo "Install complete."
echo
if [[ "${SKIP_SERVICE}" -eq 0 ]]; then
    echo "Service: ${SERVICE_NAME}"
    echo "  systemctl --user status ${SERVICE_NAME}"
    echo "  journalctl --user -u ${SERVICE_NAME} -f"
    echo "  tail -f ${LOG_FILE}"
    echo
fi
echo "Test:"
echo "  source .venv/bin/activate"
echo "  python examples/send_image.py photo.jpg"
echo
echo "Remove service:"
echo "  $(basename "$0") --remove"

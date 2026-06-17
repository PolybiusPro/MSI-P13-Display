#!/usr/bin/env bash
# Install MSI P13 display driver: native packages, vkms DRM, udev, systemd service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

SERVICE_NAME="msi-p13-panel-monitor.service"
DRIVER_ARGS="--quiet --retry-seconds 5"
UDEV_RULE="${ROOT}/scripts/99-msi-p13-display.rules"
VKMS_LOAD_CONF="/etc/modules-load.d/msi-p13-vkms.conf"
VKMS_SUDOERS="/etc/sudoers.d/msi-p13-vkms"

SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/msi-p13-display"
INSTALL_LIB="${HOME}/.local/lib/msi-p13-display"
PYTHON_SITE="${ROOT}/src"

SERVICE_TARGET="${SYSTEMD_DIR}/${SERVICE_NAME}"
CONFIG_FILE="${STATE_DIR}/install.conf"
ENV_FILE="${STATE_DIR}/environment"
DRIVER_TARGET="${INSTALL_LIB}/panel-monitor-driver.sh"
LOG_FILE="${STATE_DIR}/panel-monitor.log"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Install the MSI P13 USB display driver on Linux.

  - native system packages (Python, libusb, libdrm, vkms, kscreen)
  - PYTHONPATH=src runtime (no venv or pip install)
  - udev rule for non-root USB access
  - systemd user service (starts at graphical login)

Options:
  --remove       disable service and remove installed files
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
        python3 \
        python3-pillow python3-cryptography python3-pyusb \
        python3-dbus python3-gobject \
        libdrm kmod kernel-modules-extra \
        kscreen
}

install_deps_apt() {
    sudo apt-get update
    sudo apt-get install -y \
        python3 \
        python3-pil python3-cryptography python3-usb \
        python3-dbus python3-gi \
        libdrm2 kmod
    if apt-cache show linux-modules-extra-"$(uname -r)" >/dev/null 2>&1; then
        sudo apt-get install -y "linux-modules-extra-$(uname -r)"
    fi
    if apt-cache show kscreen >/dev/null 2>&1; then
        sudo apt-get install -y kscreen
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
            echo "Unsupported distro (${ID:-unknown}). Install python3, python3-pillow, python3-cryptography, python3-pyusb, libdrm, and vkms manually."
            exit 1
            ;;
    esac
}

install_vkms_module() {
    if ! grep -qs '^vkms$' "${VKMS_LOAD_CONF}" 2>/dev/null; then
        echo vkms | sudo tee "${VKMS_LOAD_CONF}" >/dev/null
    fi
    sudo modprobe vkms 2>/dev/null || {
        echo "warning: could not load vkms; panel monitor will try again at startup" >&2
    }
}

install_vkms_sudoers() {
    local user_name
    user_name="$(id -un)"
    printf '%s ALL=(root) NOPASSWD: /usr/sbin/modprobe -r vkms, /usr/sbin/modprobe vkms, /sbin/modprobe -r vkms, /sbin/modprobe vkms\n' \
        "${user_name}" | sudo tee "${VKMS_SUDOERS}" >/dev/null
    sudo chmod 440 "${VKMS_SUDOERS}"
    echo "Installed sudoers rule for vkms reload: ${VKMS_SUDOERS}"
}

install_python_package() {
    # Runtime uses system packages plus PYTHONPATH=src; no venv or pip install needed.
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        echo "warning: deactivate the Python venv before installing (unset VIRTUAL_ENV)" >&2
    fi
    if [[ -d "${ROOT}/.venv" ]]; then
        echo "removing stale ${ROOT}/.venv (installer uses /usr/bin/python3 and PYTHONPATH)" >&2
        rm -rf "${ROOT}/.venv"
    fi
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
PYTHON=/usr/bin/python3
PYTHONPATH=${PYTHON_SITE}
PANEL_MONITOR_MODULE=msi_p13_display.panel_monitor
PANEL_MONITOR_SCRIPT=${ROOT}/src/msi_p13_display/panel_monitor.py
DRIVER_ARGS="${DRIVER_ARGS}"
EOF
    cat >"${ENV_FILE}" <<EOF
REPO_ROOT=${ROOT}
PYTHON=/usr/bin/python3
PYTHONPATH=${PYTHON_SITE}
PANEL_MONITOR_MODULE=msi_p13_display.panel_monitor
PANEL_MONITOR_SCRIPT=${ROOT}/src/msi_p13_display/panel_monitor.py
EOF
}

install_driver_wrapper() {
    mkdir -p "${INSTALL_LIB}" "${STATE_DIR}"
    install -m 755 "${ROOT}/scripts/panel-monitor-driver.sh" "${DRIVER_TARGET}"
    write_config
}

install_systemd_service() {
    mkdir -p "${SYSTEMD_DIR}"
    cat >"${SERVICE_TARGET}" <<EOF
[Unit]
Description=MSI P13 USB display panel driver
After=graphical-session.target plasma-workspace.target
Wants=graphical-session.target plasma-workspace.target
PartOf=graphical-session.target
BindsTo=graphical-session.target

[Service]
Type=simple
EnvironmentFile=-${ENV_FILE}
ExecStart=${DRIVER_TARGET}
Restart=on-failure
RestartSec=10
TimeoutStartSec=0
PassEnvironment=WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XDG_SESSION_DESKTOP XDG_CURRENT_DESKTOP DESKTOP_SESSION HOME USER LOGNAME

[Install]
WantedBy=graphical-session.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "${SERVICE_NAME}"
    if [[ -n "${WAYLAND_DISPLAY:-}" || "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
        systemctl --user restart "${SERVICE_NAME}" || systemctl --user start "${SERVICE_NAME}"
    else
        echo "service enabled; it will start at next graphical login"
    fi
}

remove_install() {
    if [[ -f "${SERVICE_TARGET}" ]]; then
        systemctl --user disable --now "${SERVICE_NAME}" >/dev/null 2>&1 || true
        rm -f "${SERVICE_TARGET}"
        systemctl --user daemon-reload
    fi
    rm -f "${DRIVER_TARGET}" "${CONFIG_FILE}" "${ENV_FILE}"
    rmdir "${INSTALL_LIB}" 2>/dev/null || true
    echo "Removed panel monitor service and driver wrapper."
    echo "Log kept at: ${LOG_FILE}"
    echo "vkms module load config kept at: ${VKMS_LOAD_CONF}"
    if [[ -f "${VKMS_SUDOERS}" ]]; then
        sudo rm -f "${VKMS_SUDOERS}"
    fi
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
install_vkms_module
install_vkms_sudoers
install_python_package

if [[ "${SKIP_UDEV}" -eq 0 ]]; then
    install_udev_rule
fi

if [[ "${SKIP_SERVICE}" -eq 0 ]]; then
    install_driver_wrapper
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
echo "  PYTHONPATH=${PYTHON_SITE} python3 -m msi_p13_display.send_image photo.jpg"
echo
echo "Remove service:"
echo "  $(basename "$0") --remove"

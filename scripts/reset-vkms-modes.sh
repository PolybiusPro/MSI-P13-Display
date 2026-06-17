#!/usr/bin/env bash
# Reset vkms to drop stale Virtual-1 modes, then restart the panel monitor.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
fi

systemctl --user stop msi-p13-panel-monitor.service 2>/dev/null || true

modprobe -r vkms
modprobe vkms

if [[ -n "${SUDO_USER:-}" ]]; then
    sudo -u "${SUDO_USER}" systemctl --user start msi-p13-panel-monitor.service
fi

echo "vkms reset complete; panel monitor restarted for ${SUDO_USER:-$(id -un)}"

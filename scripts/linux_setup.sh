#!/usr/bin/env bash
set -euo pipefail

# Install USB/Python dependencies and create a local venv. Run from repo root.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    source /etc/os-release
fi

install_deps_apt() {
    sudo apt-get update
    sudo apt-get install -y python3-venv python3-dev libusb-1.0-0-dev
}

install_deps_dnf() {
    sudo dnf install -y python3 python3-pip python3-devel libusb-devel
}

case "${ID:-}" in
    fedora|rhel|centos)
        install_deps_dnf
        ;;
    debian|ubuntu|raspbian|linuxmint|pop)
        install_deps_apt
        ;;
    *)
        echo "Install python3, python3-venv, and libusb dev headers for your distro, then rerun."
        exit 1
        ;;
esac

# System site packages are required for KDE ScreenShot2 via python3-dbus.
python3 -m venv --system-site-packages .venv
# shellcheck source=/dev/null
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

echo
echo "USB access (run once):"
echo "  sudo cp scripts/99-msi-p13-display.rules /etc/udev/rules.d/"
echo "  sudo udevadm control --reload-rules && sudo udevadm trigger"
echo
echo "Panel monitor on KDE Wayland (Fedora example):"
echo "  sudo dnf install krfb python3-dbus python3-gobject"
echo "  ./scripts/install-panel-monitor.sh"
echo
echo "Test:"
echo "  python examples/send_image.py photo.jpg"

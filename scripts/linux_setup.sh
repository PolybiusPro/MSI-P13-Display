#!/usr/bin/env bash
set -euo pipefail

# Install native USB libraries and create a local Python environment.
# Debian/Ubuntu/Raspberry Pi OS oriented. Run from the repository root.

sudo apt-get update
sudo apt-get install -y python3-venv python3-dev libusb-1.0-0-dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

echo "For non-root access, install scripts/99-artinchip-usb-display.rules into /etc/udev/rules.d/"
echo "Then run: sudo udevadm control --reload-rules && sudo udevadm trigger"
echo "Test: python examples/draw_shapes.py --mode all"

#!/usr/bin/env bash
set -euo pipefail

# Install native USB libraries and create a local Python environment.
# Requires Homebrew. Run from the repository root.

brew install libusb
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

echo "Run: source .venv/bin/activate"
echo "Test: python examples/draw_shapes.py --mode all"

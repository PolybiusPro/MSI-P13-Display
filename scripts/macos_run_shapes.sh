#!/usr/bin/env bash
set -euo pipefail

# Run the graphics demo on macOS after scripts/macos_setup.sh.
# Extra arguments are passed to draw_shapes.py.

cd "$(dirname "$0")/.."
source .venv/bin/activate
python examples/draw_shapes.py --mode all "$@"

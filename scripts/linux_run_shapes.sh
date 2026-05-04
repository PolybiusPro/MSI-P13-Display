#!/usr/bin/env bash
set -euo pipefail

# Run the graphics demo on Linux after scripts/linux_setup.sh.
# Extra arguments are passed to draw_shapes.py.

cd "$(dirname "$0")/.."
source .venv/bin/activate
python examples/draw_shapes.py --mode all "$@"

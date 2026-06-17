#!/usr/bin/env bash
# Start the force-directed layout viewer with the live gain-tuner.
#
# Sliders re-run the REAL fdcore.simulate per change (no JS physics) — this is
# the single-source-of-truth tuner described in CLAUDE.md. Self-locating, so it
# works from any cwd; no need to `cd services/api` first.
#
#   dev/layout-sim/serve.sh            # → http://127.0.0.1:8777
#   dev/layout-sim/serve.sh --port 9000
#
# Pass --trace <fixture> instead to dump a static graph.json for the picker:
#   dev/layout-sim/serve.sh --no-serve mcu_rp2040   (see --help below)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
api="$here/../../services/api"
py="$api/.venv/bin/python"
dump="$api/scripts/dump_layout_graph.py"

if [[ ! -x "$py" ]]; then
  echo "error: backend venv not found at $py" >&2
  echo "       run: cd services/api && uv venv && uv pip install -e ." >&2
  exit 1
fi

# Passthrough escape hatch: `--no-serve <args...>` runs dump_layout_graph.py
# verbatim (e.g. to generate a static trace) instead of the live server.
if [[ "${1:-}" == "--no-serve" ]]; then
  shift
  exec "$py" "$dump" "$@"
fi

exec "$py" "$dump" --serve "$@"

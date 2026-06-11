#!/usr/bin/env bash
# Launch the freshly-built PyInstaller sidecar and confirm it answers /health.
# Catches per-OS packaging gaps (missing hidden imports, broken native deps)
# BEFORE we spend ~10 min bundling the Tauri app around a broken backend.
#
# Run after scripts/build_sidecar.sh, from any OS (macOS / Linux / Git Bash).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/apps/desktop/src-tauri/binaries/pinflow-api"
PORT="${PINFLOW_API_PORT:-8787}"

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) EXE="$BIN/pinflow-api.exe" ;;
  *) EXE="$BIN/pinflow-api" ;;
esac

echo "==> launching $EXE on port $PORT"
# No PINFLOW_PARENT_PID → the watchdog stays idle; we kill it ourselves below.
PINFLOW_API_PORT="$PORT" "$EXE" &
PID=$!

ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done

kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true

if [[ "$ok" != "1" ]]; then
  echo "!! sidecar did not answer /health within 30s — packaging is broken" >&2
  exit 1
fi
echo "==> sidecar healthy"

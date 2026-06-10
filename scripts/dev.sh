#!/usr/bin/env bash
# Boot Pinflow's full local dev loop: FastAPI service + Tauri (which boots Vite via beforeDevCommand).
# Ctrl-C cleans up both processes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_DIR="$REPO_ROOT/services/api"
DESKTOP_DIR="$REPO_ROOT/apps/desktop"

if [[ ! -d "$API_DIR/.venv" ]]; then
  echo "missing $API_DIR/.venv — run: cd services/api && uv venv && uv pip install -e ." >&2
  exit 1
fi

if [[ ! -d "$DESKTOP_DIR/node_modules" ]]; then
  echo "missing $DESKTOP_DIR/node_modules — run: cd apps/desktop && npm install" >&2
  exit 1
fi

echo "→ starting FastAPI on http://127.0.0.1:8787 (auto-reload)"
"$API_DIR/.venv/bin/uvicorn" \
  pinflow_api.main:app \
  --host 127.0.0.1 \
  --port 8787 \
  --app-dir "$API_DIR" \
  --reload \
  --reload-dir "$API_DIR" \
  --log-level info &
API_PID=$!

cleanup() {
  echo
  echo "→ stopping API (pid $API_PID)"
  kill "$API_PID" 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "→ starting Tauri (Vite + Rust)"
cd "$DESKTOP_DIR"
npm run tauri dev

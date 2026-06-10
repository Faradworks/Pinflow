#!/usr/bin/env bash
# Supervisor around scripts/dev.sh.
#
#   • Tees everything (API + Tauri + Vite) to a log file you can `tail -f`.
#   • Auto-restarts the dev loop whenever it exits — e.g. when you close the
#     Tauri window — so you get a fresh window back without re-running anything.
#   • Bails out if dev.sh dies almost immediately 3x in a row (that's a startup
#     failure — missing deps, port in use — not a window close).
#
# Stop it with:   kill "$(cat .dev-supervisor.pid)"
# (the trap tears down the whole API + Tauri + Vite tree on the way out).
#
# Override the log path with PINFLOW_DEV_LOG=/some/where.log

set -uo pipefail   # deliberately NOT -e: we must survive dev.sh failing and decide ourselves

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="${PINFLOW_DEV_LOG:-$REPO_ROOT/dev.log}"
PID_FILE="$REPO_ROOT/.dev-supervisor.pid"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# TERM a process and everything beneath it, depth-first (macOS has no setsid/--
# process-group kill we can rely on, so walk the tree by hand). This is what
# keeps uvicorn/tauri/vite/cargo from being orphaned when the supervisor stops.
kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
  trap - INT TERM EXIT
  echo "[$(ts)] supervisor stopping — tearing down dev tree" | tee -a "$LOG_FILE"
  local child
  for child in $(pgrep -P $$ 2>/dev/null); do
    kill_tree "$child"
  done
  rm -f "$PID_FILE"
  exit 0
}
trap cleanup INT TERM EXIT

echo $$ > "$PID_FILE"
echo "[$(ts)] supervisor up (pid $$) — logging to $LOG_FILE" | tee -a "$LOG_FILE"
echo "          stop with: kill $$" | tee -a "$LOG_FILE"

attempt=0
fast_fails=0
while true; do
  attempt=$((attempt + 1))
  start=$(date +%s)
  {
    echo ""
    echo "════════ [$(ts)] dev start (attempt #$attempt) ════════"
  } | tee -a "$LOG_FILE"

  # Run the dev loop, merging stderr into the log. PIPESTATUS[0] is dev.sh's code.
  "$REPO_ROOT/scripts/dev.sh" 2>&1 | tee -a "$LOG_FILE"
  code=${PIPESTATUS[0]}

  elapsed=$(( $(date +%s) - start ))
  echo "──────── [$(ts)] dev exited (code $code) after ${elapsed}s ────────" | tee -a "$LOG_FILE"

  # A healthy run lasts until you close the window (many seconds+). A sub-8s exit
  # is almost always a startup failure; 3 in a row → stop instead of spin-looping.
  if [[ $elapsed -lt 8 ]]; then
    fast_fails=$((fast_fails + 1))
    if [[ $fast_fails -ge 3 ]]; then
      echo "[$(ts)] dev exited in <8s three times running — looks like a startup failure. Giving up; see the log above." | tee -a "$LOG_FILE"
      break
    fi
  else
    fast_fails=0
  fi

  echo "[$(ts)] restarting in 2s …  (kill $$ to stop)" | tee -a "$LOG_FILE"
  sleep 2
done

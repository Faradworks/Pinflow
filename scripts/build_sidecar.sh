#!/usr/bin/env bash
# Build the Pinflow FastAPI backend as a PyInstaller --onedir bundle and stage
# it into the Tauri resource dir (apps/desktop/src-tauri/binaries/pinflow-api).
#
# Runs identically on macOS, Linux and Windows (Git Bash) — the release CI
# (.github/workflows/release.yml) calls it on each native runner because
# PyInstaller cannot cross-compile. Local macOS builds reach it via
# scripts/build_desktop.sh.
#
# --onedir (NOT --onefile): onefile re-unpacks the whole ~65 MB bundle to a temp
# dir on EVERY launch (~10 s cold start); onedir keeps the files extracted next
# to the executable, so startup is ~1 s. The onedir tree ships as a Tauri
# resource (see tauri.conf.json `bundle.resources`) and is spawned from
# src-tauri/src/lib.rs, which resolves it under Resources/binaries/pinflow-api/.
#
# A dedicated build venv (.venv-build) is used so this never clobbers the dev
# .venv that scripts/dev.sh relies on.
#
# Prereqs: `uv` on PATH (https://github.com/astral-sh/uv).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$ROOT/services/api"
BIN="$ROOT/apps/desktop/src-tauri/binaries"
VENV="$API/.venv-build"

echo "==> create build venv (uv, Python 3.12)"
cd "$API"
uv venv "$VENV" --python 3.12
uv pip install --python "$VENV" -e . pyinstaller

# pip installs the console entry point under bin/ (POSIX) or Scripts/ (Windows).
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) PYI="$VENV/Scripts/pyinstaller" ;;
  *) PYI="$VENV/bin/pyinstaller" ;;
esac

echo "==> PyInstaller (--onedir)"
"$PYI" --name pinflow-api --onedir --noconfirm --clean \
  --collect-all kicad_sch_api --collect-all kipy --collect-all anthropic \
  --collect-submodules uvicorn \
  --copy-metadata anthropic --copy-metadata fastapi --copy-metadata uvicorn \
  pinflow_api/__main__.py

echo "==> stage the onedir into the Tauri resource dir"
rm -rf "$BIN/pinflow-api"
mkdir -p "$BIN"
cp -R "$API/dist/pinflow-api" "$BIN/pinflow-api"

echo "==> sidecar staged at $BIN/pinflow-api"

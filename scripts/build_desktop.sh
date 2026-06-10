#!/usr/bin/env bash
# Build the Pinflow desktop app (.app + .dmg) with the bundled FastAPI sidecar.
#
# The sidecar is packaged with PyInstaller **--onedir**, NOT --onefile: onefile
# re-unpacks the whole ~65 MB bundle to a temp dir on EVERY launch (~10 s cold
# start); onedir keeps the files extracted next to the executable, so startup is
# ~1 s. The onedir tree is shipped as a Tauri resource (see tauri.conf.json
# `bundle.resources`) and spawned from src-tauri/src/lib.rs via std::process,
# which resolves it under Resources/binaries/pinflow-api/.
#
# (First launch of the freshly-built UNSIGNED binary pays a one-time macOS
# code-validation of its dylibs, ~10 s, cached by content afterward. Signing +
# notarization removes even that.)
#
# Prereqs: services/api/.venv with deps + pyinstaller; apps/desktop npm install.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$ROOT/services/api"
BIN="$ROOT/apps/desktop/src-tauri/binaries"

echo "==> PyInstaller (--onedir)"
cd "$API"
.venv/bin/pyinstaller --name pinflow-api --onedir --noconfirm --clean \
  --collect-all kicad_sch_api --collect-all kipy --collect-all anthropic \
  --collect-submodules uvicorn \
  --copy-metadata anthropic --copy-metadata fastapi --copy-metadata uvicorn \
  pinflow_api/__main__.py

echo "==> stage the onedir into the Tauri resource dir"
rm -f  "$BIN"/pinflow-api-*-apple-darwin   # drop any stale onefile binary
rm -rf "$BIN/pinflow-api"
mkdir -p "$BIN"
cp -R "$API/dist/pinflow-api" "$BIN/pinflow-api"

echo "==> tauri build"
cd "$ROOT/apps/desktop"
npm run tauri build

echo "==> done → apps/desktop/src-tauri/target/release/bundle/"

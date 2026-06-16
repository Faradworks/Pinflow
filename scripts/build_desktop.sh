#!/usr/bin/env bash
# Build the Pinflow desktop app (.app + .dmg) locally on macOS, with the bundled
# FastAPI sidecar.
#
# The sidecar build (PyInstaller --onedir, staged into src-tauri/binaries/) lives
# in scripts/build_sidecar.sh so the same step runs unchanged on every OS in CI
# (.github/workflows/release.yml). This wrapper just builds the sidecar and then
# runs `tauri build` for the local Mac bundle.
#
# (First launch of the freshly-built UNSIGNED binary pays a one-time macOS
# code-validation of its dylibs, ~10 s, cached by content afterward. Signing +
# notarization removes even that.)
#
# Prereqs: `uv` on PATH; apps/desktop `npm install` done.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"$ROOT/scripts/build_sidecar.sh"

echo "==> tauri build"
cd "$ROOT/apps/desktop"
# --config merges the build-only sidecar resource overlay (see
# tauri.bundle.conf.json); it's kept out of the base config so `tauri dev` works.
npm run tauri build -- --config src-tauri/tauri.bundle.conf.json

echo "==> done → apps/desktop/src-tauri/target/release/bundle/"

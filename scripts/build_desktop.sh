#!/usr/bin/env bash
# Build the Pinflow desktop app (.app + .dmg) locally on macOS, with the bundled
# FastAPI sidecar.
#
# The sidecar build (PyInstaller --onedir, staged into src-tauri/binaries/) lives
# in scripts/build_sidecar.sh so the same step runs unchanged on every OS in CI
# (.github/workflows/release.yml). This wrapper just builds the sidecar and then
# runs `tauri build` for the local Mac bundle.
#
# The bundle is ad-hoc signed (no Apple Developer account yet): the sidecar's
# nested binaries in build_sidecar.sh, the outer .app via tauri.conf.json
# bundle.macOS.signingIdentity "-", plus a final `--deep` re-sign + verify below.
# That stops the fatal "damaged, move to Trash" Gatekeeper dialog on Apple
# Silicon but does NOT notarize — a downloaded copy still needs the user to
# right-click → Open once (see README "Installing on macOS").
#
# (First launch of the freshly-built binary pays a one-time macOS code-validation
# of its dylibs, ~10 s, cached by content afterward. Notarization removes even
# that.)
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

# Ad-hoc deep re-sign the finished .app + strict verify. `tauri build` already
# signs the outer bundle and the sidecar was signed in build_sidecar.sh; this
# `--deep` pass guarantees nothing in the tree is left unsigned, which is the
# difference between Gatekeeper's fatal "damaged" dialog and the recoverable
# "unidentified developer" one. No Apple Developer account needed (`-` = ad-hoc).
APP="$(/usr/bin/find "$ROOT/apps/desktop/src-tauri/target" -maxdepth 4 -name 'Pinflow.app' -path '*/release/bundle/macos/*' -print -quit)"
if [[ -n "${APP:-}" ]]; then
  echo "==> ad-hoc deep re-sign $APP"
  codesign --force --deep --sign - --timestamp=none "$APP"
  codesign --verify --deep --strict "$APP"
fi

echo "==> done → apps/desktop/src-tauri/target/release/bundle/"

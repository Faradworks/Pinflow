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

# Optional cross-arch build: on macOS, set PINFLOW_PY_ARCH=x86_64 to freeze an
# Intel sidecar on an Apple Silicon host (x86_64 Python run under Rosetta 2), so
# the macOS Intel installer can be built on a plentiful arm64 runner instead of
# GitHub's scarce Intel runners. Unset → build for the host arch.
# (Plain string, not a bash array, so the empty case is safe under macOS bash 3.2.)
PY_REQUEST="3.12"
PYI_ARCH_FLAG=""
if [[ "$(uname -s)" == "Darwin" && "${PINFLOW_PY_ARCH:-}" == "x86_64" ]]; then
  echo "==> cross-arch: building an x86_64 sidecar via Rosetta 2"
  PY_REQUEST="cpython-3.12-macos-x86_64-none"
  PYI_ARCH_FLAG="--target-architecture x86_64"
fi

echo "==> create build venv (uv, Python: $PY_REQUEST)"
cd "$API"
uv venv --clear "$VENV" --python "$PY_REQUEST"
# Force a prebuilt cryptography wheel. On the cross-arch path (x86_64 interpreter
# under Rosetta on an arm64 runner) uv otherwise falls back to a source build,
# which drags in maturin + a Rust openssl-sys compile that fails for lack of
# OpenSSL (see release.yml macos-x64). The x86_64 macOS wheel exists; just use it.
uv pip install --python "$VENV" --only-binary cryptography -e . pyinstaller

# pip installs the console entry point under bin/ (POSIX) or Scripts/ (Windows).
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) PYI="$VENV/Scripts/pyinstaller" ;;
  *) PYI="$VENV/bin/pyinstaller" ;;
esac

echo "==> PyInstaller (--onedir)"
"$PYI" --name pinflow-api --onedir --noconfirm --clean $PYI_ARCH_FLAG \
  --collect-all kicad_sch_api --collect-all kipy --collect-all anthropic \
  --collect-submodules uvicorn \
  --copy-metadata anthropic --copy-metadata fastapi --copy-metadata uvicorn \
  pinflow_api/__main__.py

echo "==> stage the onedir into the Tauri resource dir"
rm -rf "$BIN/pinflow-api"
mkdir -p "$BIN"
cp -R "$API/dist/pinflow-api" "$BIN/pinflow-api"

# Ad-hoc codesign the staged sidecar on macOS (we don't have an Apple Developer
# cert yet — `-` is the ad-hoc identity, no account required). Without this, the
# nested Mach-O files are unsigned: on Apple Silicon the kernel kills the spawned
# `pinflow-api` launcher and dyld rejects the dlopen'd .so/.dylib files, so the
# app boots to a dead backend. The launcher must be signed AFTER its nested
# libraries so the seal covers them. The outer .app is signed separately —
# `tauri build`/tauri-action via bundle.macOS.signingIdentity "-" in CI, and a
# `--deep` re-sign in scripts/build_desktop.sh locally. Remove the ad-hoc path
# (here + that config key) once notarization with a real Developer ID lands.
if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "==> ad-hoc codesign the staged sidecar (no Apple Developer cert yet)"
  find "$BIN/pinflow-api" -type f \( -name '*.so' -o -name '*.dylib' \) \
    -exec codesign --force --sign - --timestamp=none {} +
  codesign --force --sign - --timestamp=none "$BIN/pinflow-api/pinflow-api"
fi

echo "==> sidecar staged at $BIN/pinflow-api"

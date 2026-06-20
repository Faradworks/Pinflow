# Releasing Pinflow

Cross-platform desktop installers are built by
[`.github/workflows/release.yml`](../.github/workflows/release.yml). Each OS is
built on its own native runner because the bundled PyInstaller sidecar (and
Tauri's webview linkage) cannot cross-compile.

## What gets built

| Target | Runner | Installer(s) |
| --- | --- | --- |
| macOS Apple Silicon | `macos-14` | `.dmg` (aarch64) |
| macOS Intel | `macos-14` (Rosetta cross-build) | `.dmg` (x86_64) |
| Windows x64 | `windows-latest` | `.exe` (NSIS) + `.msi` |
| Linux x64 | `ubuntu-22.04` | `.AppImage` + `.deb` |

The Intel macOS leg is cross-built on an Apple Silicon runner (Rust `x86_64`
target + an `x86_64` PyInstaller sidecar frozen under Rosetta 2, via
`PINFLOW_PY_ARCH=x86_64`) to avoid GitHub's scarce Intel runners.

Each leg: freeze the FastAPI sidecar with PyInstaller
([`scripts/build_sidecar.sh`](../scripts/build_sidecar.sh)) → smoke-test it
against `/health` ([`scripts/smoke_sidecar.sh`](../scripts/smoke_sidecar.sh)) →
bundle the Tauri app → attach to the release.

The bundled sidecar is declared as a Tauri resource in a **build-only config
overlay** ([`tauri.bundle.conf.json`](../apps/desktop/src-tauri/tauri.bundle.conf.json)),
merged in with `tauri build --config …`. It's kept out of the base
`tauri.conf.json` so `tauri dev` — which doesn't freeze the sidecar — doesn't
fail on the missing resource. The local equivalent of a release build is
[`scripts/build_desktop.sh`](../scripts/build_desktop.sh).

## macOS code signing + notarization

The macOS legs are signed with a **Developer ID Application** certificate and
notarized by Apple, so downloaded builds open without Gatekeeper warnings. The
sidecar's nested Mach-O binaries are signed (hardened runtime + secure
timestamp) before `tauri build` signs the outer `.app`; `tauri-action` then
submits to the notary service and staples the ticket.

This needs the following **repository secrets** (set under Settings → Secrets
and variables → Actions):

| Secret | What it is |
| --- | --- |
| `APPLE_CERTIFICATE` | base64 of the Developer ID Application `.p12` |
| `APPLE_CERTIFICATE_PASSWORD` | password for that `.p12` |
| `APPLE_SIGNING_IDENTITY` | `Developer ID Application: <Name> (<TEAMID>)` |
| `APPLE_API_KEY_P8` | base64 of the App Store Connect API key `.p8` |
| `APPLE_API_ISSUER` | App Store Connect API issuer UUID |
| `APPLE_API_KEY` | App Store Connect API key ID |

The signing identity is read from the secret (not committed); the macOS signing
config is generated at build time and falls back to ad-hoc signing when the
secret is absent, so dry-run builds without secret access still succeed
(unsigned). When the repo is public (or on a paid plan), move these into a
`release` **environment** with a required reviewer to gate the cert behind
approval — the workflow already runs tag builds in that environment.

> Windows is not yet Authenticode-signed (see Deferred).

## Cut a release

1. Bump the version in `apps/desktop/src-tauri/tauri.conf.json` (the source of
   truth for installer filenames). Keep `Cargo.toml` / `package.json` in step if
   you care about them matching.
2. Commit, then tag with a matching `v` prefix and push:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

   The workflow verifies the tag matches `tauri.conf.json` and **fails the build
   if they disagree**, so a mismatched tag can't publish.
3. The workflow publishes a **draft** GitHub Release with all installers
   attached. Review it, then hit *Publish*.

## Dry run (no release)

Use the **Run workflow** button (Actions → Release → Run workflow) on any
branch. It builds every OS and uploads the installers as **workflow artifacts**
(downloadable from the run summary) without creating a release.

## First-launch warnings

- **macOS** — signed + notarized (see above): opens with no warning. A build
  produced without the signing secrets (e.g. a fork dry-run) is unsigned —
  right-click the app → **Open** (once), or `xattr -dr com.apple.quarantine
  /Applications/Pinflow.app`.
- **Windows** — not yet Authenticode-signed, so SmartScreen shows "Windows
  protected your PC." Click **More info → Run anyway**.
- **Linux** — no warning; `chmod +x` the AppImage if your file manager hasn't.

## Deferred (future work)

- **Windows Authenticode signing** — to clear the SmartScreen warning
  (`tauri-action` supports a Windows cert via secrets).
- **Auto-updater** — Tauri's updater plugin + signed update artifacts.

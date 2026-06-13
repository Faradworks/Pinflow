# Releasing Pinflow

Cross-platform desktop installers are built by
[`.github/workflows/release.yml`](../.github/workflows/release.yml). Each OS is
built on its own native runner because the bundled PyInstaller sidecar (and
Tauri's webview linkage) cannot cross-compile.

## What gets built

| Target | Runner | Installer(s) |
| --- | --- | --- |
| macOS Apple Silicon | `macos-14` | `.dmg` (aarch64) |
| macOS Intel | `macos-13` | `.dmg` (x86_64) |
| Windows x64 | `windows-latest` | `.exe` (NSIS) + `.msi` |
| Linux x64 | `ubuntu-22.04` | `.AppImage` + `.deb` |

Each leg: freeze the FastAPI sidecar with PyInstaller
([`scripts/build_sidecar.sh`](../scripts/build_sidecar.sh)) → smoke-test it
against `/health` ([`scripts/smoke_sidecar.sh`](../scripts/smoke_sidecar.sh)) →
bundle the Tauri app → attach to the release.

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

## Unsigned builds — first-launch warnings

These installers are **not yet code-signed or notarized**, so the OS will warn
on first launch:

- **macOS** — "Pinflow can't be opened because Apple cannot check it for
  malicious software." Right-click the app → **Open** (once), or clear the
  quarantine flag:

  ```bash
  xattr -dr com.apple.quarantine /Applications/Pinflow.app
  ```

- **Windows** — SmartScreen shows "Windows protected your PC." Click **More
  info → Run anyway**.

- **Linux** — no warning; `chmod +x` the AppImage if your file manager hasn't.

## Deferred (future work)

- **macOS Developer ID signing + notarization** and **Windows Authenticode** —
  `tauri-action` supports both via repo secrets (`APPLE_CERTIFICATE`,
  `APPLE_ID`, `APPLE_TEAM_ID`, … / Windows cert). Drop them in and the
  first-launch warnings go away.
- **Auto-updater** — Tauri's updater plugin + signed update artifacts.

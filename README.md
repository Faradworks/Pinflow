# Pinflow

<img width="1728" height="1080" alt="pinflow_demo" src="https://github.com/user-attachments/assets/a4d0c2b5-c5ab-4c65-bda8-a286172da16c" />


An open-source agentic assistant for electronics design — automating the tedious
parts of schematic capture, layout, and component selection so engineers can
focus on design intent.

## Vision

Pinflow is an AI-native co-designer for hardware engineers. It works as a
companion to [KiCad](https://www.kicad.org/) — where designers already are —
with a chat-driven agent on one side and a live schematic view on the other.

## What it does today

The desktop app is a chat-driven agent shell: the conversation on the left, a
live KiCanvas schematic viewer on the right. You talk to an agent that can read
your open KiCad schematic, draft and place subcircuits, resolve real orderable
parts, and **stage** edits for you to review before they touch your files. Many
tools are still being filled in.

## Planned capabilities

- **Auto schematic generation** — describe a subsystem or drop a datasheet, and
  Pinflow drafts the subcircuit with sensible part choices, decoupling, and net
  naming.
- **Component placement & routing** — agentic placement that respects
  analog/digital separation and signal integrity.
- **Component intelligence** — pin-aware part lookup, footprint checks, and
  context-aware alternatives.
- **Design-rule + intent reasoning** — catch issues a linter can't, by
  understanding what the circuit is *trying* to do.

## Quick start

**Prerequisites:** KiCad 10, Python 3.10+, Node.js, Rust (for Tauri), and
[`uv`](https://github.com/astral-sh/uv).

```bash
# Backend (FastAPI service)
cd services/api
uv venv && uv pip install -e .

# Frontend (Tauri + React)
cd ../../apps/desktop
npm install
```

**Bring your own key (BYOK).** Pinflow runs entirely on your own Anthropic API
key — the BYOK build is the complete product. Either enter your key in the app's
first-run onboarding screen, or set it in `services/api/.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Then boot the app (starts the FastAPI service + the Tauri desktop shell):

```bash
./scripts/dev.sh
```

Optionally, **Pinflow Cloud** is a hosted, managed-key + parts-catalogue add-on
you can sign into from within the app — but it's just a convenience on top of
the open-source app.

## Installing on macOS

> **Heads-up:** Pinflow isn't notarized by Apple yet (our Apple Developer
> account is in progress). The app is ad-hoc signed, so it runs fine — but
> because macOS can't verify the developer, the **first** launch needs one extra
> click. This is temporary; a notarized build will remove this step entirely.

On first launch you'll see **"Apple could not verify 'Pinflow' is free of
malware"** with **Move to Trash** / **Done** buttons. This is expected for a
non-notarized app — **don't click Move to Trash.** Approve it once, using
whichever path matches your macOS version:

- **macOS 15 (Sequoia) and newer:** click **Done**, then open **System Settings
  → Privacy & Security**, scroll to the *Security* section, and click **Open
  Anyway** next to "Pinflow was blocked…". Authenticate, then confirm **Open
  Anyway**. (Apple removed the old right-click → Open shortcut in Sequoia.)
- **macOS 14 (Sonoma) and earlier:** in Finder, right-click (or Control-click)
  `Pinflow.app`, choose **Open**, then **Open** again in the dialog.
- **Any version, from Terminal** — clear the download quarantine flag to skip the
  dialog entirely:

  ```bash
  xattr -dr com.apple.quarantine /Applications/Pinflow.app
  ```

macOS remembers your approval, so every launch after the first is a normal
double-click.

> If you instead see **"Pinflow is damaged and can't be opened"** (with *only* a
> Move to Trash button), the download was corrupted or partially extracted —
> re-download the `.dmg` and try again. That wording means the bundle failed its
> signature check outright, which a complete download of a current build should
> not do.

## Integrations

- **KiCad** — reads your open project and stages schematic edits you review
  before committing. Nothing is written to your files without an explicit
  accept.
- **Standalone** — a path for headless use and for users not in KiCad.

## Development

Build and run steps are in [Quick start](#quick-start) above. When the agent
misbehaves on a prompt, `services/api/scripts/trace_chat.py` runs the loop
in-process with a full-fidelity trace tap (every prompt, response, and complete
tool input/output); run it with `--help` for usage.

## Contributing

Contributions are welcome! See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[GNU General Public License v3.0 or later](./LICENSE).

## Maintainers

Built by [Faradworks](https://github.com/faradworks).

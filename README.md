# Pinflow

<img width="1728" height="1080" alt="pinflow_demo" src="https://github.com/user-attachments/assets/a4d0c2b5-c5ab-4c65-bda8-a286172da16c" />

Pinflow is an open-source AI assistant for schematic design in [KiCad](https://www.kicad.org/). It pairs a chat-driven agent with a live schematic view, helping you draft subcircuits and pick real, orderable parts without leaving your workflow.

> Note: Pinflow works on schematics. It does not do PCB layout or routing.

## What it does

The desktop app is a chat workspace: the conversation on the left, a live [KiCanvas](https://kicanvas.org/) schematic viewer on the right. The agent can:

- Read the KiCad schematic you have open.
- Draft and place subcircuits from a plain-language description.
- Resolve real, orderable parts with the correct pinout and footprint.
- Stage every edit for review. Nothing is written to your files until you accept it.

Pinflow is under active development, and more agent tools are being added.

## Install on macOS

Download the latest build from the [Releases page](https://github.com/Faradworks/Pinflow/releases):

- Apple Silicon (M1 and newer): `Pinflow_<version>_aarch64.dmg`
- Intel: `Pinflow_<version>_x64.dmg`

Open the `.dmg` and drag Pinflow into your Applications folder. If macOS reports that the app cannot be verified, right-click it and choose Open, or run:

```bash
xattr -dr com.apple.quarantine /Applications/Pinflow.app
```

On first launch, enter your Anthropic API key in the onboarding screen (see [Bring your own key](#bring-your-own-key) below).

## Bring your own key

Pinflow runs entirely on your own Anthropic API key. The BYOK build is the complete product. Enter your key on first launch, or set it in `services/api/.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Pinflow Cloud is an optional hosted add-on (managed key plus a parts catalogue) that you can sign into from inside the app. It is a convenience layer on top of the open-source app, not a requirement.

## Build from source

Prerequisites: KiCad 10, Python 3.10+, Node.js, Rust (for Tauri), and [`uv`](https://github.com/astral-sh/uv).

```bash
# Backend (FastAPI service)
cd services/api
uv venv && uv pip install -e .

# Frontend (Tauri + React)
cd ../../apps/desktop
npm install
```

Boot the app, which starts the FastAPI service and the Tauri desktop shell:

```bash
./scripts/dev.sh
```

## How it connects to KiCad

Pinflow reads the project you have open and stages schematic edits for you to review. Nothing is written to your files without an explicit accept. A standalone path supports headless use and users who are not working in KiCad.

## Roadmap

- Datasheet to subcircuit: drop in a datasheet and have Pinflow draft the subcircuit with sensible part choices, decoupling, and net naming.
- Deeper part intelligence: pin-aware lookup, footprint checks, and context-aware alternatives.
- Design intent checks: catch issues a linter cannot, by reasoning about what the circuit is trying to do.

## Development

Build and run steps are in [Build from source](#build-from-source) above. When the agent misbehaves on a prompt, `services/api/scripts/trace_chat.py` runs the loop in-process with a full-fidelity trace tap that records every prompt, response, and complete tool input and output. Run it with `--help` for usage.

## Contributing

Contributions are welcome. See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[GNU General Public License v3.0 or later](./LICENSE).

## Maintainers

Built by [Faradworks](https://github.com/faradworks).

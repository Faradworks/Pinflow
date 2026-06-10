# Pinflow

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

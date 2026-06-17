# Pinflow

Open-source, AI-native co-designer for hardware engineers — a companion to **KiCad** that automates
schematic capture, subcircuit layout, and part selection. The product is a desktop app: a chat-driven
agent on the left, a live KiCad schematic view on the right. The agent reads your open schematic,
drafts/places subcircuits, resolves real orderable parts, and **stages** edits for you to review
before anything touches your files.

**BYOK** (bring your own key): runs on your own Anthropic API key (`services/api/.env` →
`ANTHROPIC_API_KEY`, or the app's onboarding). "Pinflow Cloud" is an optional managed-key + parts
add-on layered on top.

## Repo layout

| Path | What |
|---|---|
| `apps/desktop/` | The product. Tauri (Rust shell) + React/Vite front-end; embeds KiCanvas for the schematic view. The Python backend ships as a packaged **sidecar**. |
| `apps/docs/` | Public docs site — Nextra (Next.js). |
| `services/api/` | The brains: a FastAPI service (`pinflow_api/`). Almost all real logic lives here. |
| `scripts/` | Top-level dev/build orchestration (`dev.sh`, `build_sidecar.sh`, `build_desktop.sh`). |
| `dev/` | Experimental dev tooling (e.g. `dev/layout-sim/` — the force-directed placement viewer; see its own `CLAUDE.md`). |
| `docs/`, `assets/` | Project docs and static assets. |

## Backend architecture (`services/api/pinflow_api/`)

FastAPI app in `main.py`; HTTP surface in `routes/` (`agent`, `schematic`, `datasheet`, `generate`,
`kicad`, `chips`, `cloud`, `auth`, `health`). The desktop app talks to this service over HTTP.

Key subsystems:

- **`agent/`** — the agentic loop (`loop.py`) the chat drives: tool dispatch (`tools/`), conversation
  `state.py`, streamed `events.py`, `schematic_sync.py` (keeps the live view in sync). `staging.py`
  holds edits pending user review.
- **`emit/`** — the placement/emission pipeline: a `Netlist` → a placed `(kicad_sch ...)` document.
  This is the most algorithm-dense area.
  - `netlist.py` (intermediate format) → `classify.py` + `layout_tree.py` (group parts into
    archetypes: cap banks, dividers, bootstraps…).
  - `placers/` — engines behind a registry (`placers/__init__.py`, `get_placer`/`place`):
    `cplace` (default, constraint-based per-axis solver), `fdplace` (experimental force-directed —
    see `dev/layout-sim/CLAUDE.md`), `greedy`, `legacy`, `llm_placer`. `DEFAULT_PLACER = "auto"`
    (cplace, falling to greedy on the dense-IC pattern).
  - `route.py` (crossing-minimising orthogonal wire router), `rubric.py` (scores layout quality —
    the optimization target), `render.py` (`.kicad_sch`/S-expr → PNG via `kicad-cli`).
- **Generation pipeline** — `datasheet_parse.py` → `design_spec.py` → `netlist_synth.py` → `emit`
  (place) → S-expression handed to KiCad.
- **Parts & symbols** — `parts.py`/`parts_client.py` (catalogue/MPN resolution),
  `symbol_resolver.py`/`sym_lib*.py` (KiCad symbol libraries), `easyeda.py` (fallback symbol fetch).
- **LLM** — `llm.py`/`llm_emit.py` (Anthropic SDK; **default to the latest Claude models**),
  `cost.py` (token metering / spend cap).
- **KiCad bridge** — `kicad_cli.py`, `kicad_detect.py`. KiCad (target **v10**) has *no* schematic
  plugin API; edits reach KiCad via the OS clipboard S-expression, project-local files, and
  `kicad-cli`. Nothing is written without an explicit user accept.

`kicad-sch-api` (imported as `ksa`) is the library used to read/build schematic S-expressions.

## Commands

```bash
# One-time setup
cd services/api && uv venv && uv pip install -e .      # backend (Python 3.10+, uv)
cd apps/desktop && npm install                          # front-end (Node, Rust/Tauri)

# Run the whole app (FastAPI service + Tauri shell)
./scripts/dev.sh

# Backend: always use the venv interpreter, from services/api/
cd services/api
.venv/bin/python scripts/check_all.py        # THE offline regression gate (runs all the below)
.venv/bin/python scripts/eval_layout.py --placer cplace [--only NAME] [--render]   # layout scores
.venv/bin/python scripts/check_determinism.py --placer cplace                       # byte-stability
.venv/bin/python scripts/test_route.py       # the test suite is scripts/test_*.py (no pytest)
.venv/bin/python scripts/trace_chat.py --help   # replay the agent loop with a full trace tap

# Front-end build check (CONTRIBUTING expects this to pass)
cd apps/desktop && npm run build
```

## Conventions & gotchas

- **Run Python via `services/api/.venv/bin/python`, from `services/api/`.** Scripts assume that cwd.
- **`check_all.py` is the gate.** It enforces per-golden layout **score floors** (in
  `check_all.py` and `tests/fixtures/generated_corpus.json`) plus smoke scripts. Run it before/after
  any change to `emit/` or the agent. Never lower a floor to make a regression pass.
- **Placement must stay deterministic** — golden floors depend on it. KiCad regenerates random UUIDs
  per emit, so compare layouts with the **UUID-normalized** hash (`check_determinism.py`), never raw
  bytes.
- **Golden netlist fixtures are derived/gitignored** (`tests/fixtures/golden/*.netlist.json`).
  Regenerate with `scripts/sch_to_netlist.py <golden>.kicad_sch` (or just run `check_all.py`).
- **Match surrounding style.** This codebase favors dense, intent-explaining docstrings/comments
  (the *why*, not the *what*) and small composable helpers. Mirror it.
- **LLM work:** use the latest Claude models; respect `cost.py` metering.
- **Match the house style** in commits/PRs per `CONTRIBUTING.md`; keep changes focused.

## Pointers

- `dev/layout-sim/CLAUDE.md` + `STATUS.md` — the force-directed placement experiment (`fdplace`).
- `README.md` — product vision, quick start. `CONTRIBUTING.md` — PR basics.

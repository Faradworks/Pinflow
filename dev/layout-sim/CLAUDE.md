# Force-directed schematic placement — working context

Experimental placement engine that relaxes parts under physical forces instead of cplace's one-shot
constraint solver. Goal: if it matches/beats cplace on the rubric corpus it becomes the primary
placer, and unlike cplace it generalizes to multi-IC. **Current state + what's left: see `STATUS.md`.**

This dir (`dev/layout-sim/`) is the debug viewer. The engine itself lives under `services/api/`.

## File map

| File | Role |
|---|---|
| `services/api/pinflow_api/emit/fdcore.py` | **The physics.** Pure, no `kicad_sch_api`. `simulate(nodes, edges, cfg)`. Single source of truth — the placer AND this viewer run the same `simulate`. |
| `services/api/pinflow_api/emit/placers/fdplace.py` | The placer. Mirrors `cplace._build_once`'s scaffold, swaps the constraint solve for `fdcore.simulate`, then snaps + labels, then reuses cplace's router/serialize. `trace_layout()` produces this viewer's JSON. |
| `services/api/scripts/dump_layout_graph.py` | `--trace` runs the real sim and dumps `{graph, frames, snapped}`. |
| `dev/layout-sim/index.html` | Canvas playback (scrubber, gate-box overlay) on the left; a **snapped frames** toggle (default on) animates the placer's *finalized* layout per frame (grid + structural `_snap_groups`/`_align_snap`/anti-stack), so the animation converges to exactly what the placer emits; in `--serve` mode a split pane on the right shows the **real kicad-cli render** of the settled layout (auto-refreshed per gain change via `/api/render`). Pure rendering — no JS physics; `trace_layout` precomputes each frame's `snapped` positions. |
| `dev/layout-sim/graph.json` | Generated trace (gitignore-able; regenerate any time). |

Modified production files (small): `cplace.py` (extracted `_horiz_refs`, shared), `placers/__init__.py`
(registered `"fdplace"`), `scripts/check_determinism.py` (added `--placer`).

## Architecture & key decisions

- **Single source of truth = `fdcore`.** It's kicad-free so the viewer animates the *exact* sim the
  placer runs — no physics twin to drift. Don't reimplement forces in JS.
- **Pipeline: forces → snap → label.** (1) `fdcore.simulate` places parts (star attraction to net
  centroids, AABB-overlap repulsion = the symbol_overlap-gate gradient, gravity power↑/gnd↓, role-based
  L→R flow; damped-Euler + cooling). The relaxation is **grid-exact**: `SimConfig.snap_grid` (default
  **on**) hard-snaps every free node to the 1.27 grid after each integration step (velocity stays
  continuous), so what you watch settling *is* the placed result — no end-of-run shift. (2)
  `fdplace._snap_groups`/`_finalize_positions` make the *structural* craft metrics crisp (divider/shunt
  columns, even-pitch banks, anti-stack); with `snap_grid` on the trailing 1.27 grid-round is a no-op.
  (3) `fdplace._place_labels` places text. **Labels are cosmetic and NEVER move a part** (per Sid).
  The viewer's **snap grid** toggle drives `SimConfig.snap_grid` live (off = legacy continuous relax +
  one-shot snap). Corpus impact (fdplace, snap on vs off): net-neutral mean (+0.001) — buck_tps62840
  +0.031, mt3608 −0.014, tps61088 −0.013, rest ≈0; `test_no_ic_wires` + fdplace determinism stay green.
- **The seam.** A placer only fills `placed_refs[ref]=(x,y)` (+ rotations); everything downstream
  (router `emit/route.py`, no-connects, serialize) is reused verbatim. `fdplace._build_once` mirrors
  `cplace._build_once`; only the middle (solve→positions) differs.
- **The rubric is the energy function.** `emit/rubric.py` metrics map to forces/snaps:
  rail_proximity↔attraction, symbol_overlap↔repulsion, alignment/spacing/chain↔snaps, flow↔flow field.
- **v1 scope:** single-IC; the IC is pinned at `(IC_X, IC_Y)` and orientation is reused from cplace's
  `_orient_all` (forces decide position only). Multi-IC + force-driven orientation = v2 (see STATUS).

## Run it

```bash
# viewer + live tuner, one shot, from any cwd (no venv activation / cd needed):
./dev/layout-sim/serve.sh                 # → http://127.0.0.1:8777  (--port N to override)

cd services/api
# scoreboard
.venv/bin/python scripts/eval_layout.py --manifest tests/fixtures/generated_corpus.json --placer fdplace
.venv/bin/python scripts/eval_layout.py --placer fdplace                       # hand-drawn goldens
# a rendered picture → services/api/_renders/<name>.fdplace.png
.venv/bin/python scripts/eval_layout.py --only buck_tps62840 --manifest tests/fixtures/generated_corpus.json --placer fdplace --render
# determinism (must stay green)
.venv/bin/python scripts/check_determinism.py --placer fdplace

# viewer (static): trace a circuit, then serve this dir and open it
.venv/bin/python scripts/dump_layout_graph.py mcu_rp2040 --trace --out ../../dev/layout-sim/graph.json
cd ../../dev/layout-sim && python3 -m http.server 8777      # → http://localhost:8777

# viewer (live tuner): sliders re-run the REAL fdcore.simulate per change
.venv/bin/python scripts/dump_layout_graph.py --serve        # → http://127.0.0.1:8777
```

Wiring: the production router (`emit/route.py`, shared by cplace + fdplace) leaves every pin a colinear
**stub** along its away-direction and drops a `(junction)` dot where stubs tap a shared rail — default
on. The viewer's **grid (mm)** combobox (KiCad presets) sets the snap quantum *and* the stub unit; the
**stub (grid)** slider sets the stub length in grid units (`/api/render?grid=&stub_units=`).

The live tuner (`--serve`) adds an `/api/trace` endpoint the viewer's gain sliders
hit on every change; the server runs `fdplace.trace_layout` with the requested
gains and the page re-animates. No JS physics — the single-source-of-truth rule
holds. This is how `repel_aniso` was tuned. A second endpoint, `/api/render`, runs
the *full* placer (`fdplace` → snap → route → label) and rasterises the emitted
`.kicad_sch` via `kicad-cli` (`emit/render.py`), so the right-hand pane shows what
KiCad actually draws for the current gains — debounced after the trace and aborted
when a knob moves again.

Golden netlist fixtures (`tests/fixtures/golden/*.netlist.json`) are gitignored/derived; regenerate
with `scripts/sch_to_netlist.py <golden>.kicad_sch` if missing (or run `check_all.py`).

## Gotchas (these bit me — heed them)

- **Determinism is a hard requirement** (the score-floors depend on it) and KiCad regenerates random
  UUIDs every emit. Compare with the **UUID-normalized** hash (`check_determinism.py`), never raw
  bytes. fdcore stays deterministic via sorted iteration (`_nkey`), derived init, fixed iters, pure
  Python floats — keep it that way (no `set`/`dict` iteration in the hot loop, no numpy reductions).
- **Global gain tuning is sensitive and non-monotonic.** Sweeping one gain trades fixtures
  unpredictably (flow 0.5→5/7, 1.0→2/7, 1.5→4/7). Prefer structural/snap fixes and fixture-specific
  gating (e.g. the dense-IC gate in `_snap_groups`) over chasing global gains. The exception that
  proves the rule: `repel_aniso=1.25` was a clean Pareto win (mcu_rp2040 .66→.93, nothing regressed)
  precisely because it's *structural* — it biases *which axis* resolves an overlap, not a force
  magnitude — so it can't blow the system up the way scaling `repel`/`attract` does. Magnitudes
  trade; axis preference just rotates the same settled energy. (Found via the `--serve` tuner.)
- **Don't regress cplace.** `fdplace` is additive; `auto`/`DEFAULT_PLACER` are untouched on purpose.
  Promotion (M4) must be gated behind a full `check_all.py` pass.
- **label_collision is text-on-text only** (rubric excludes same-owner and text-on-wire). The label
  placer optimizes overlap vs. net labels + other fields — not bodies.

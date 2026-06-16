# Test fixtures

Vendor PDFs are gitignored (IP). Regenerate locally:

```bash
curl -sL -o tps62843_datasheet.pdf \
  https://www.ti.com/lit/ds/symlink/tps62843.pdf
```

## Schematics

- `rp2040.kicad_sch`, `tps628436.kicad_sch` — golden outputs from Pinflow's own
  builders. Tracked. Not hand-drawn — useful as **structural** regression inputs
  (`structural_diff.validate_placer_output`), not as visual-quality references.
- `golden/*.kicad_sch` — **hand-drawn** layout-quality references (see "Layout-
  quality corpus" below).

## Derived netlist exports

`*.netlist.json` + `*.netlist.symbols/` are the placer's `Netlist` IR exported
from the `.kicad_sch` above via `scripts/sch_to_netlist.py`. The `.kicad_sch`
files are the source of truth; the derived netlist exports are gitignored —
regenerate on demand:

```bash
cd services/api
.venv/bin/python scripts/sch_to_netlist.py tests/fixtures/rp2040.kicad_sch
.venv/bin/python scripts/sch_to_netlist.py tests/fixtures/golden/tps63020.kicad_sch
```

## Layout-quality corpus

`golden_corpus.json` is the manifest read by the layout-quality harness,
`scripts/eval_layout.py` (and `eval_llm_placer.py`): for each listed golden it
scores the hand-drawn `.kicad_sch` against the rubric
(`pinflow_api.emit.rubric`), regenerates from the same netlist with the chosen
placer, scores that, and prints the per-metric gap.

Four hand-drawn goldens under `golden/`, spanning the LDO / boost / buck-boost
topologies — `ams1117` LDO, `mt3608` + `tps61088` boost, `tps63020` buck-boost.
Each `golden/*.kicad_sch` was added by hand; its `.netlist.json` +
`.netlist.symbols/` are derived by `scripts/sch_to_netlist.py` (regenerate with
that script if a `.kicad_sch` changes). Add an entry to the manifest as more
goldens are curated.

## Generated (prompt-derived) corpus

`generated/*.netlist.json` + `generated_corpus.json` are the **realistic**
placer inputs: the messier netlists the chat agent actually emits, where layout
quality breaks down. The goldens above are clean and hand-drawn, so they don't
reproduce that — this corpus does.

Unlike the golden netlists (derived on demand from a committed `.kicad_sch`, and
gitignored), these `.netlist.json` files **are committed as the source of
truth** — an LLM-synthesized netlist can't be regenerated deterministically, so
the captured artifact is what we keep. Two kinds of entry, tagged by `source`:

- **`hand-authored`** — deterministic, agent-shaped netlists (rails marked
  `is_port`, bundled `lib_id`s only). No API key needed; the invariant layer.
- **`captured`** — frozen real output of the netlist-first agent chain
  (`parse_datasheet` → `design_spec` → `netlist_synth`). Capture once:

  ```bash
  cd services/api
  .venv/bin/python scripts/capture_netlist.py TPS62840 \
      --topology buck --vin +5V --vout +3V3 --vref 0.6 \
      --fsw-hz 2400000 --iout-a 0.5 --role "buck regulator"
  # cold cache: add --pdf <datasheet>.  Needs ANTHROPIC_API_KEY (BYOK).
  ```

  It writes `generated/<name>.netlist.json`, self-checks that it places, and
  prints the manifest line to paste into `generated_corpus.json`. If the
  resolved symbol isn't bundled (`source: easyeda`), add a
  `<name>.netlist.symbols/` sidecar next to the JSON (same shape as the golden
  sidecars) so the fixture replays on a clean checkout.

Iterate on the placer against this corpus (render to eyeball, score with the
rubric, swap engines via the `emit.placers` registry):

```bash
cd services/api
.venv/bin/python scripts/eval_layout.py \
    --manifest tests/fixtures/generated_corpus.json --render          # all, cplace
.venv/bin/python scripts/eval_layout.py \
    --manifest tests/fixtures/generated_corpus.json --only buck_tps62840 \
    --placer greedy --render                                          # one, another engine
```

`scripts/test_generated_corpus.py` is the gate (wired into `check_all.py`): per
entry it asserts the netlist places, validates, is deterministic, and stays at
or above its manifest `score_floor`. Set a floor to the measured rubric total
minus ~0.02 jitter; raise it when a change improves an entry, never lower it to
make a regression pass.

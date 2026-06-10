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

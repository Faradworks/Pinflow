# Force-directed placement — status

**Branch:** `feature/force-directed-schematic` · **Snapshot:** 2026-06-16
**One line:** a force-directed placer (`fdplace`) is built, integrated, deterministic, and passes the
rubric floor on **5/7** corpus circuits (from 0). It is **not yet used in production** — `auto` and
`DEFAULT_PLACER` are untouched, so nothing in the live product calls it.

Plan of record: `~/.claude/plans/you-can-choose-the-keen-glacier.md` (milestones M1–M4).

## Scoreboard (`--placer fdplace`, current gains)

| circuit | score | floor | |
|---|---|---|---|
| ams1117 | 1.000 | 0.98 | ✅ |
| ldo_ap2112k | 0.998 | 0.98 | ✅ (a real captured agent output) |
| buck_tps62840 | 0.975 | 0.95 | ✅ |
| tps61088 | 0.949 | 0.82 | ✅ (dense-IC case cplace defers to greedy) |
| mt3608 | 0.918 | 0.90 | ✅ |
| tps63020 | 0.954 | 0.98 | −0.026 |
| mcu_rp2040 | 0.916 | 0.93 | −0.014 |

Reproduce: see `CLAUDE.md` → "Run it".

## Done

- [x] **M1 — engine + integration.** `emit/fdcore.py` (pure physics) + `emit/placers/fdplace.py`
      (wrapper). Registered as `"fdplace"`. Produces valid schematics: connectivity + symbol_overlap
      gates pass; router emits 0 crossings / 0 diagonals / 0 wires-through-parts on the fixtures.
- [x] **Determinism.** Byte-stable across runs (UUID-normalized). `check_determinism.py --placer fdplace`
      is green for all goldens. cplace verified unregressed by the `_horiz_refs` extraction.
- [x] **Coherence snaps** (`fdplace._snap_groups`) — divider compaction, shunt-branch columns,
      even-pitch cap banks (gated off dense-IC layouts).
- [x] **Label pass** (`fdplace._place_labels`) — collision-aware, moves text only, never parts.
      This was the big score unlock (tps61088 +0.077, tps63020 +0.10, ams1117 back to perfect).
- [x] **M3 — viewer.** `dump_layout_graph.py --trace` + `dev/layout-sim/index.html`. Plays back the
      real sim frames; orthogonal stub-wires from pin facings; gate-overlap overlay; show-snapped.

## Left

- [ ] **mcu_rp2040 (−0.014)** — pure `flow`: its bulk input cap wants to sit left but the +3V3 rail
      (spans the whole MCU) attracts it back. Global flow-gain sweeps don't fix it (and are erratic —
      see judgment calls). Needs a *targeted* rule (e.g. exempt a single bulk-cap-on-spanning-rail
      from flow, or seed it left).
- [ ] **tps63020 (−0.026)** — one `wire_through_part`: a wire clips a body. A routing artifact from
      one part's position; needs a small post-route nudge for the offending part, not global tuning.
- [ ] **M4 — multi-IC (v2).** `fdcore` already supports it in principle (pin largest IC, free the
      rest, `reorient_every` for greedy orientation). Needs: a multi-IC fixture authored (none exist),
      the `fdplace` v2 branch (skip `_orient_all`, use `classify._assign_hosts`), and validation.
- [ ] **M4 — promotion.** Wire `fdplace` into `_load_auto`, **gated to the cases cplace punts**
      (zero/multi-IC) so it's additive and can't regress the floors. Behind a full `check_all.py` pass.
- [ ] **(v2) force-driven orientation.** Today v1 reuses cplace's `_orient_all`; forces decide
      position only. Letting pin-facing forces choose orientation is the bigger, truer step.

## Judgment calls to revisit (flagged for review)

1. v1 borrows cplace's **orientation** (`_orient_all`); forces do position only. Not yet "pure" FD.
2. The **IC is a repulsion obstacle** so supports settle around it, not on it (small deviation from
   strict gate-only repulsion; improves layout).
3. **Forces → snap → label** split: forces get parts to the right neighborhood; deterministic snaps
   make the grid-exact craft metrics (alignment/spacing/chain) crisp. Decide if that split is OK or
   the forces alone should be tighter.
4. **Global gain tuning is sensitive and non-monotonic** (flow 0.5→5/7, 1.0→2/7, 1.5→4/7). Prefer
   structural/snap fixes and fixture-specific gating over global gain sweeps.

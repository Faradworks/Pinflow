# Force-Directed Placer (fdplace) — Status & Next-Step Handoff

_Branch: `feature/force-directed-schematic`. Last updated mid-session; written so work can resume cold._

## TL;DR — current state

**STEP 1 (`wire_coverage` metric), STEP 2 (short fix), STEP 3 (passive keep-out + de-cram) are all
DONE.** Both ams1117 and tps61088 wire; passives no longer overlap or get sliced as badly. ams1117 is
now a clean **1.000**. What remains is the dense-IC quality on tps61088 (§8).

- STEP 1 added `wire_coverage` so the all-labels fallback stopped scoring a fake 1.000 (§5 STEP 1).
- STEP 2 fixed the short — **NOT** the router collinear-wire §4 originally claimed, but a **GND
  power-symbol drop landing on a foreign pin** (§4, §5 STEP 2).
- STEP 3 (§5 STEP 3): a **soft passive keep-out** in the router (it had only an IC keep-out) so wires
  deter cutting through caps/resistors, plus an **inter-group column de-collision** in
  `fdplace._snap_groups` so a chain column never overlaps another group (the D2/C23 diode-on-cap).
  fdplace: ams1117 0.914→**1.000**, tps63020 0.820→0.876, tps61088 0.671→0.686 (improved, dense).
  Shared router code also lifts cplace mt3608 0.959→1.000.

"You can't tune what you can't measure."

---

## 1. What this work is

`fdplace` = an experimental **force-directed** schematic placer (alternative to the production
constraint placer `cplace`). The IC is pinned; support parts relax under a physics sim (`fdcore`),
then snap to grid; the result is wired by the shared router and serialized to a `.kicad_sch`.

Goal of recent work: make fdplace's output **use clean local wires**, with **no wires through the
IC body**, and **labels only for genuinely cross-chip nets** — like a human-drawn schematic.

## 2. Commits on this branch (newest first)

| Commit | What |
|---|---|
| `2aad5c2` | **Side-assignment**: a one-sided x-only "side-bias" force in `fdcore` pushes each support part past the IC edge on the side its pin serves (input-left/output-right). Pin-aware anti-stack. Reuses `_side_from_contacts`. |
| `4be23f5` | **Relabel IC-spanning nets as labels**: route-then-relabel — any net whose routed path spans/cuts the IC becomes net labels instead of a wire (`_net_crosses_ic` in `netlist_to_sch._place_connectivity`). Guarantees 0 wires through the IC. |
| `70e9588` | **Keep-out detour candidates** in the router (`route._keepout_detours`) so it can route *around* the IC. |
| `c72c05a` | Feature checkpoint: the placer + the browser debug viewer (`dev/layout-sim/`) + router **IC keep-out** + **honest `wire_through_part`** metric (traversal-based). |

All four build on each other. The branch gate (`check_all.py`) is **red on pre-existing score
floors** (from the honest-metric change) — judge changes by **per-entry rubric deltas**, not pass/fail.

## 3. What works vs what's broken

Per-fixture fdplace rubric total, post STEP-1 (`wire_coverage`) + STEP-2 (short fix) + STEP-3
(passive keep-out & de-cram). Both fallbacks wire; passives no longer overlap / get sliced as badly:

| fixture | total | wired? | notes |
|---|---|---|---|
| buck_tps62840 | 0.916 | ✅ 17 wires | clean: input-left, output-right, VOUT a label (spans). The hero case. |
| mt3608 | 1.000 | ✅ 14 wires | clean. |
| tps63020 | 0.876 | ✅ wired | STEP 3: `wire_through_part` 4→1 (passive keep-out). Was 0.820. |
| **ams1117** | **1.000** | ✅ wired | STEP 3 de-cram separated the D2/R12 column from C23 → no body overlap, label_collision 2→0. Was 0.914. |
| **tps61088** | **0.686** | ✅ wired | STEP 3: `wire_through_part` 8→5 (but +1 crossing). Dense 20-pin — *improved*, not clean. Was 0.671. |

Hard invariants still hold everywhere (both placers, both corpora): **0 wires through any IC**
(`scripts/test_no_ic_wires.py` — all clear), and **deterministic** (`scripts/check_determinism.py`,
both placers). The STEP-3 passive keep-out is shared router code, so it also re-routes some cplace
wires — **net-positive**: cplace mt3608 0.959→**1.000** (one fewer through-passive), others hold.

## 4. The short — CORRECTED diagnosis (this section's original claim was wrong)

When the emitted schematic fails the connectivity check, `fdplace()` falls back to `wiring="labels"`
(short-safe, **draws no wires**). STEP 2 fixed what caused that failure.

**Original (WRONG) diagnosis — kept as a caution:** that the crossing-min router drew a *collinear
wire down a single-column series chain* (C23→D2→R12) passing over intermediate pins. Repro **refutes
this**: ground is emitted *outside* the router (the `is_gnd` branch in
`netlist_to_sch._place_connectivity` drops each GND cluster to a `power:GND` symbol and `continue`s,
never reaching `route_nets`), and `route_nets` reports **0 foreign overlaps** on both fixtures. Also
ams1117's SHUNT_BRANCH is just `{D2,R12}` — C23 is a separate RAIL_CAP_BANK — so the "series chain
collinear wire" can't occur.

**Actual root cause:** a **GND power-symbol drop landing on a foreign pin.** The drop was a straight
`sym_xy = (xy[0], xy[1] + GND_STUB)` (`netlist_to_sch.py`, GND_STUB=2.54). When a foreign pin sits
one stub away in that direction, the drop wire's endpoint coincides with it → the two nets short
(both fixtures' merge sets confirm GND shorting to a neighbour net). ams1117: the D2/R12 shunt column
grid-snaps onto C23's x, so C23's GND drop lands on D2's pin. tps61088: dense cap packing puts each
GND pin one stub above the next cap's `+5V` pin. The code already handled this for *side-facing IC*
ground pins (the `away` step-out) but not for a down-drop onto a foreign part's pin.

Repro oracle (the connectivity gate is the same check): `_topo_diff(_netlist_topology(nl),
_export_topology(res.sch_text))` returns `[]` once fixed; before the fix it listed the merged net.

## 5. The fix plan (in order)

### STEP 1 — anti-fallback rubric metric — ✅ DONE (`wire_coverage`)
Added `wire_coverage` to `rubric.py`: fraction of **IC pins** drawn with a wire (a wire-segment
**endpoint** coincident with the placed pin) vs a bare net label, scored `min(1, coverage/0.1)`.
Weight `0.13` in `_WEIGHTS` (peer of `wire_crossings`/`wire_through_part`); registered in `score()`
inside the `if netlist is not None:` block. Effect (fdplace): ams1117 1.000→**0.876**,
tps61088 0.929→**0.820**; wired fixtures stay high (buck 0.916, mt3608 1.000, tps63020 0.820).
No new `check_all` failures — the two reds (tps63020 0.962<0.98 golden, buck 0.935<0.95 generated)
are pre-existing from the honest-`wire_through_part` change; `wire_coverage=1.0` actually nudged
both *up* (verified vs a clean-tree baseline).

**Two as-built corrections to the original design sketch (both load-bearing):**
1. **Geometry, not net-state.** `score(sch_text, netlist)` gets **no `PlacerResult`** (no `.issues`/
   `.label_specs`), so wired-vs-label is detected geometrically. Loaded components have an **empty
   `.pins`** list, so `extract_pinmap` can't be used — placed pin coords come from
   `ksa.get_symbol_info(lib_id).pins` (lib-local offsets) + the **exact `netlist_to_sch._pin_xy`
   transform** (Y-flip, rotate by `-rot`, translate to origin). Reproducing that transform is what
   makes a placed pin coincide to float-epsilon with the wire endpoint the emitter drew. The plan's
   `ic_contacts[].pin.x/y` are in the **canonical (100,100) pinmap frame** — unusable directly; join
   to placed coords by **pin number** instead. See `_placed_ic_pins`.
2. **Count ALL IC pins, not signal-only** (overrides the "exclude rails/ground" advice below). ams1117
   is a 3-terminal LDO with **zero** signal IC nets, so a signal-only denominator scores it `None`
   and the headline fallback stays invisible. Counting all pins catches it; the **ramp** (not net-kind
   exclusion) absorbs idiomatic labels.
3. **Ramp hugs zero (`0.1`), not the sketch's `0.8`.** Under the all-pins denominator a *good* dense
   MCU legitimately labels most pins (rp2040 = 0.20 coverage = 2/10 wired), while the fallback is
   *wholesale* (exactly 0.0 — the emitter commits all routed wires or none). `0.5` wrongly cratered
   rp2040 (0.956→0.885, below its 0.93 floor); `0.1` clears every legit fixture (≥0.20) with margin
   and still craters the 0.0 fallbacks. The empirical coverage spread is bimodal: `{0.0}` (fallback)
   vs `[0.20 … 1.0]` (wired).

This metric also guards against future placement changes silently re-triggering the fallback.

_(Original sketch kept for context — superseded where it conflicts with the as-built notes above:
 a signal-only `wire_coverage` with an ≥80% target. The signal-only scope and 80% target were both
 wrong for this corpus; see corrections 2–3.)_

### STEP 2 — fix the short — ✅ DONE (GND-drop foreign-pin avoidance)
The fix was NOT the placement-stagger / router-jog the original §4 sketch implied (those target a
router collinear wire that doesn't exist). It was a **GND-symbol-drop redirect** in the `is_gnd`
branch of `_place_connectivity` (`netlist_to_sch.py`), generalizing the pre-existing side-pin
step-out: before committing a `power:GND` symbol, test the drop (endpoint + drop-wire) against every
**foreign-net** pin (`all_pins` minus the dropping net's own pins, via `_drop_hits_foreign`); on a
collision, pick the first clear direction (`_DIR_VEC`, priority `default → D,L,R,U → 2× default
stub`); else fall back to the old default. The *default direction is tried first and computed
bit-identically*, so a well-spaced layout (every cplace golden) is **unchanged** — the redirect only
fires on a real cross-net collision.

Result: ams1117 & tps61088 both **wire** (connectivity gate passes); ams1117 0.876→0.914,
tps61088 0.820→0.671 (honest — exposes its real passive-through-part / label defects, §8). cplace
byte-identical (4/4 golden hashes unchanged); both placers deterministic; 0 through-IC wires;
`test_route` green; no new `check_all` failures. Single-file change in `netlist_to_sch.py`
(plus importing `route._interior`).

_(Original sketch — placement stagger in `_snap_groups` SHUNT_BRANCH, or a router `_candidates`
off-axis jog — is **superseded**: the short was never a routed wire. Don't pursue it.)_

### STEP 3 — passive keep-out + de-cram — ✅ DONE
Two paired changes (the renders showed passives both *sliced by wires* and *overlapping each other*).

- **Router passive keep-out (shared `route.py` + `netlist_to_sch.py`).** The router only ever got the
  IC as a keep-out; passive bodies were never passed. New `_passive_bodies` (next to `_ic_keepouts`)
  builds non-IC body rects; `_wire_router` gains a `bodies=` param passed to `route_nets` as **soft**
  obstacles (`_W_BODY`, raised 4→10 == `_W_CROSS`). Soft, not hard: pins sit on the box *edge* and
  `_seg_hits_rect` is strict-interior, so horizontal fan-out to parallel parts is never flagged — only
  a wire *traversing* a body is. No body-detour candidates needed (existing L/Z candidates + the
  higher weight suffice). Shared code → also re-routes some cplace wires (net-positive: mt3608 →1.000).
- **Inter-group de-cram (`fdplace._snap_groups`, fdplace-only).** A deterministic post-pass pushes each
  movable chain column (SHUNT_BRANCH/DIVIDER_STACK) **outward** past every occupied x-interval (seeded
  from *all* non-movable parts, incl. single-member cap banks like C23 that the `len<2` guard never
  `handled` — the root cause of the D2/C23 overlap). Whole column moves as one `dx` → chain_coherence
  untouched; outward-only → dense-IC staircase safe; no collision → inert.

Result: ams1117 0.914→**1.000** (D2/C23 separated, label_collision 2→0), tps63020 0.820→0.876
(through 4→1), tps61088 0.671→0.686 (through 8→5, +1 crossing — dense, only improved). cplace holds
or improves (mt3608 0.959→1.000). Both placers deterministic; 0 through-IC; `test_route` green; the
only `check_all` reds (tps63020, buck) are pre-existing.

## 6. Key files & functions

- `services/api/pinflow_api/emit/fdcore.py` — physics core (kicad-free). `simulate()` loop;
  forces `_attraction`/`_repulsion`/`_fields`/`_side_bias`; `Node` (has `.side`); `DEFAULT_GAINS`
  (`side=1.5`). Deterministic (no RNG).
- `services/api/pinflow_api/emit/placers/fdplace.py` — `fdplace()` entry (router → fallback to
  labels), `_build_once`, `_build_graph` (builds nodes, computes `_target_side`), `_finalize_positions`
  (side clamp → group snap → grid snap → **pin-aware anti-stack**), `_snap_groups` (archetype
  compaction incl. SHUNT_BRANCH columns), `trace_layout` (debug-viewer payload).
- `services/api/pinflow_api/emit/netlist_to_sch.py` — `_place_connectivity` (the wire-vs-label
  decision: routes nets, relabels IC-spanning ones via `_net_crosses_ic`, falls to labels on
  topology fail); `_side_from_contacts` (modal IC side per part — reused by side-assignment);
  `_ic_keepouts`; `_topology_intact`.
- `services/api/pinflow_api/emit/route.py` — crossing-min router. `route_nets`/`_score`
  (`_W_OVERLAP`, `_W_KEEPOUT`, `_W_CROSS`); `_keepout_detours`; `_interior` (pin-over detection).
- `services/api/pinflow_api/emit/rubric.py` — scoring. `score()`, `_WEIGHTS`, `count_ic_through_wires`,
  `_gate_connectivity` (kicad-cli export vs netlist), `_wire_segments`, `_body_boxes`.
- `services/api/pinflow_api/emit/structural_diff.py` — `_export_topology`, `_netlist_topology`,
  `_topo_diff` (find merged/split nets — how the short was diagnosed).
- `dev/layout-sim/index.html` — browser debug viewer; `scripts/dump_layout_graph.py <fixture> --trace
  --out dev/layout-sim/graph.json` feeds it.

## 7. How to run / verify (from `services/api`, use `.venv/bin/python`)

```bash
# rubric + render one fixture (goldens use default manifest; buck needs --manifest)
.venv/bin/python scripts/eval_layout.py --placer fdplace --only tps61088 --render
.venv/bin/python scripts/eval_layout.py --placer fdplace --manifest tests/fixtures/generated_corpus.json --only buck_tps62840 --render
# hard invariant: 0 wires through any IC, both placers, both corpora
.venv/bin/python scripts/test_no_ic_wires.py
.venv/bin/python scripts/check_determinism.py        # must stay "deterministic"
.venv/bin/python scripts/test_route.py               # router unit tests
.venv/bin/python scripts/check_all.py                # full gate — RED on pre-existing floors; judge by deltas
```
Renders land in `services/api/_renders/<name>.<placer>.png` (gitignored).

**GOTCHA — symbol-cache pollution:** the goldens load sidecar symbol libs that **shadow bundled
libs** of the same name (e.g. mt3608's `Regulator_Switching` shadows the bundled one buck needs).
In a single process, run **generated entries first** (bundled libs, no discovery) then goldens, OR
**re-discover each fixture's sidecar immediately before running it**. `clear_cache()` does NOT reset
library paths. `scripts/test_no_ic_wires.py` already orders correctly (generated-first) — copy that.

The 5 fixtures: goldens `ams1117, mt3608, tps61088, tps63020` (in `golden_corpus.json`, have
hand-drawn ceilings + sidecar symbols); generated `buck_tps62840, mcu_rp2040, ldo_ap2112k` (in
`generated_corpus.json`, netlist-only, bundled symbols).

## 8. Known issues / open items
- **ams1117** & **tps61088** fallbacks: **FIXED** (STEP 2). **Passive keep-out + de-cram: DONE**
  (STEP 3). ams1117 is now a clean **1.000**.
- **tps61088 layout quality (top open item):** still the hardest — wires at **0.686** after STEP 3
  (`wire_through_part` 8→5, +1 crossing; label collisions remain). The dense 20-pin / 7-group cohort
  needs more than one de-cram pass (cap-bank-vs-cap-bank spacing, label placement). STEP 3 only moves
  SHUNT_BRANCH/DIVIDER_STACK columns; cap banks are fixed anchors.
- **tps63020**: improved to **0.876** (through 4→1) by the STEP-3 passive keep-out; residual 1
  through-passive + 1 crossing remain.
- **cplace through-passive residuals** (e.g. C19/R8 on crammed goldens): the shared passive keep-out
  only helps where a detour exists; crammed cplace cases need cplace-side spacing (out of scope so far).
- `check_all.py` red on score floors (tps63020, buck) since the honest `wire_through_part` change —
  **pre-existing**, unchanged by STEPs 1–3. Re-baseline the floors only after the layout quality
  settles (floors are in `check_all.py` SCORE_FLOORS and the manifest `score_floor` fields).
- Untracked, intentionally never committed: root `CLAUDE.md`, `services/api/tests/fixtures/golden/.history/`.
- Side-bias gain is `1.5` (tuned on the 5 fixtures; buck strongly prefers it). Sweep code pattern is
  in the session history if re-tuning (remember the symbol-cache gotcha).

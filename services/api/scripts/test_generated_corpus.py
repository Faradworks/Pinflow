"""Invariant + score-floor gate for the prompt-derived netlist corpus.

The companion to the golden-corpus floors in `check_all.py`, for the
`generated_corpus.json` fixtures (hand-authored + captured agent output).
These netlists have no hand-drawn ceiling, so the gate is two-part:

  1. INVARIANTS (hard, per entry) — the netlist self-validates, the default
     placer places every part, `validate_placer_output` passes, placement is
     deterministic, and no two non-power symbols land on identical coordinates
     (coincident pins silently merge nets — the 2026-06-12 retry-spiral cause;
     see scripts/test_agent_netlist.py).
  2. SCORE FLOOR (soft, when `score_floor` is set) — the rubric total must not
     fall below the floor recorded in the manifest, so a placer change that
     degrades a realistic topology fails here, not in a user's schematic.

No LLM, no network — deterministic from the committed JSON.

Run: cd services/api && .venv/bin/python scripts/test_generated_corpus.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import kicad_sch_api as ksa  # noqa: E402

from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.placers import get_placer  # noqa: E402
from pinflow_api.emit.rubric import score  # noqa: E402
from pinflow_api.emit.structural_diff import validate_placer_output  # noqa: E402

FIXTURES = API_DIR / "tests" / "fixtures"
MANIFEST = FIXTURES / "generated_corpus.json"

# Same coincident-symbol detector as test_agent_netlist.py: non-power symbols
# sharing identical (x, y) means stacked parts → coincident pins → merged nets.
_SYM_RE = re.compile(
    r'\(symbol\s+\(lib_id "[^"]+"\)\s+\(at ([\d.-]+) ([\d.-]+) \d+\)'
    r'.*?\(property "Reference" "([^"]+)"', re.S)


def _stacked(sch_text: str) -> list[tuple[str, str]]:
    seen: dict[tuple[str, str], str] = {}
    dups: list[tuple[str, str]] = []
    for x, y, ref in _SYM_RE.findall(sch_text):
        if ref.startswith("#PWR"):
            continue
        if (x, y) in seen:
            dups.append((seen[(x, y)], ref))
        seen[(x, y)] = ref
    return dups


def _check(entry: dict) -> str | None:
    """Return a failure string, or None when the entry passes every check."""
    name = entry["name"]
    if entry.get("symbols"):
        try:
            ksa.get_symbol_cache().discover_libraries(
                [str(FIXTURES / entry["symbols"])])
        except Exception as e:  # noqa: BLE001
            return f"{name}: could not register sidecar symbols: {e}"

    nl_path = FIXTURES / entry["netlist"]
    nl = Netlist.model_validate(json.loads(nl_path.read_text()))

    self_errs = nl.validate_self()
    if self_errs:
        return f"{name}: netlist self-validation failed: {'; '.join(self_errs)}"

    placer = get_placer()  # default (production) engine
    result = placer(nl, title=name)

    placed = len(result.placed_refs)
    if placed != len(nl.parts):
        return f"{name}: placed {placed}/{len(nl.parts)} parts"

    vr = validate_placer_output(nl, result)
    if not vr.ok:
        return f"{name}: validate_placer_output failed: {'; '.join(vr.errors)}"

    stacked = _stacked(result.sch_text)
    if stacked:
        return f"{name}: stacked symbols at identical coords: {stacked}"

    again = placer(nl, title=name)
    if again.placed_refs != result.placed_refs:
        return f"{name}: nondeterministic placement (placed_refs differ across runs)"

    rb = score(result.sch_text, nl)
    floor = entry.get("score_floor")
    detail = f"total={rb.total:.3f}"
    if floor is not None:
        detail += f" floor={floor}"
        if rb.total < floor:
            return f"{name}: rubric {rb.total:.3f} fell below floor {floor}"
    print(f"[PASS] {name}: {detail}  parts={len(nl.parts)} placed={placed} "
          f"validates deterministic", flush=True)
    return None


def main() -> int:
    if not MANIFEST.is_file():
        print(f"error: manifest not found at {MANIFEST}", file=sys.stderr)
        return 1
    entries = json.loads(MANIFEST.read_text()).get("entries", [])
    if not entries:
        print("no entries in generated_corpus.json (nothing to gate)")
        return 0

    failures = [f for f in (_check(e) for e in entries) if f]
    if failures:
        print(f"\n{len(failures)} FAILURE(S):", file=sys.stderr)
        for f in failures:
            print("  -", f, file=sys.stderr)
        return 1
    print(f"\nall {len(entries)} generated-corpus entries pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

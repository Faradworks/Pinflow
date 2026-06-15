"""Run every offline (no-LLM, no-network) check in one shot.

The regression gate for agent/placer changes. Runs the smoke scripts, then
the golden-corpus layout eval with PER-GOLDEN SCORE FLOORS — the floors are
the measured scores at the time a change lands minus a small jitter
allowance, so a placer "fix" that quietly degrades another topology fails
here instead of in a user's schematic.

Regenerates the derived netlist fixtures if missing (they're gitignored;
the golden .kicad_sch files are the source of truth).

Run: cd services/api && .venv/bin/python scripts/check_all.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent
PY = sys.executable
FIXTURES = API_DIR / "tests" / "fixtures"

SMOKE_SCRIPTS = [
    "scripts/test_agent_netlist.py",
    "scripts/test_ask_user_dedup.py",
    "scripts/test_cost.py",
    "scripts/test_cost_loop.py",
    "scripts/check_determinism.py",
    "scripts/test_route.py",
]

# Measured on 2026-06-12 (branch fix/agent-loop-quality) minus 0.02 jitter.
# Raise a floor when a change improves a golden; never lower one to make a
# regression pass.
SCORE_FLOORS = {
    "ams1117": 0.98,
    "mt3608": 0.90,
    "tps61088": 0.82,
    "tps63020": 0.98,
}


def run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    cp = subprocess.run(cmd, cwd=API_DIR, capture_output=True, text=True,
                        timeout=timeout)
    return cp.returncode, (cp.stdout + cp.stderr)


def main() -> int:
    failures: list[str] = []

    # Derived netlist fixtures (gitignored) — regenerate when absent.
    goldens = sorted((FIXTURES / "golden").glob("*.kicad_sch"))
    for sch in goldens:
        if not sch.with_suffix("").with_suffix(".netlist.json").exists():
            print(f"regenerating netlist fixture for {sch.name} ...", flush=True)
            rc, out = run([PY, "scripts/sch_to_netlist.py", str(sch)])
            if rc != 0:
                failures.append(f"sch_to_netlist {sch.name}: rc={rc}\n{out[-400:]}")

    for script in SMOKE_SCRIPTS:
        rc, out = run([PY, script])
        status = "PASS" if rc == 0 else "FAIL"
        print(f"[{status}] {script}", flush=True)
        if rc != 0:
            failures.append(f"{script}: rc={rc}\n{out[-600:]}")

    rc, out = run([PY, "scripts/eval_layout.py", "--json"])
    if rc != 0:
        failures.append(f"eval_layout: rc={rc}\n{out[-600:]}")
    else:
        try:
            # eval_layout --json prints {"reports": [...]} (possibly after
            # noise lines from kicad-cli); find the object start.
            payload = out[out.index("{"):]
            reports = json.loads(payload).get("reports", [])
        except ValueError as e:  # covers json.JSONDecodeError + str.index miss
            reports = []
            failures.append(f"eval_layout: could not parse --json output: {e}")
        for r in reports:
            name = r.get("name")
            score = (r.get("regen") or {}).get("total")
            floor = SCORE_FLOORS.get(name)
            if score is None or floor is None:
                continue
            status = "PASS" if score >= floor else "FAIL"
            print(f"[{status}] eval {name}: {score:.3f} (floor {floor})",
                  flush=True)
            if score < floor:
                failures.append(
                    f"eval {name}: {score:.3f} fell below floor {floor}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f.splitlines()[0])
        return 1
    print("\nall offline checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

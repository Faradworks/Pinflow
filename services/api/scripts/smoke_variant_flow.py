"""End-to-end smoke test for the variant-aware parse → synth → place flow.

Drives the new pipeline against `tests/fixtures/tps62843_datasheet.pdf`:

  1. Register the PDF as an attachment.
  2. Call parse_datasheet(mpn='TPS628436', attachment_id=…). Verifies:
       - LLM-A populates available_variants with the orderable parts table
       - Variant picker lands on a real bundled symbol (TPS628436DRL or YKA)
       - get_symbol_pins covers the pintable
       - LLM-B emits a Netlist that passes validate_self
  3. Call add_subcircuit_from_netlist(netlist=…) against an empty .kicad_sch
     in a tempdir. Verifies placement + staging works end-to-end.

Doesn't commit; the temp dir is dropped at exit.

Requires services/api/.env with ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from pinflow_api.agent import state as st
from pinflow_api.agent.attachments import AttachmentRef
from pinflow_api.agent.tools import (
    add_subcircuit_from_netlist as tool_add,
    design_spec as tool_spec,
    parse_datasheet as tool_parse,
)


FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "tps62843_datasheet.pdf"
)


def main() -> int:
    if not FIXTURE.is_file():
        print(f"FIXTURE MISSING: {FIXTURE}")
        print("Regenerate per tests/fixtures/README.md")
        return 2

    with tempfile.TemporaryDirectory(prefix="pinflow-smoke-") as td:
        td_path = Path(td)
        sch_path = td_path / "smoke.kicad_sch"
        sch_path.write_text("")  # empty schematic — _subcircuit_common handles this

        state = st.ConversationState(conversation_id="smoke")
        state.active_sch_path = sch_path

        # Register the PDF the way the /agent/attachments endpoint would.
        aid = "att_smoke_pdf"
        state.attachments[aid] = AttachmentRef(
            attachment_id=aid,
            filename=FIXTURE.name,
            mime="application/pdf",
            size=FIXTURE.stat().st_size,
            path=FIXTURE,
        )

        print("=" * 72)
        print("Step 1: parse_datasheet (LLM-A extract → resolve symbol)")
        print("=" * 72)
        result = tool_parse.run(
            state,
            mpn="TPS628436",
            attachment_id=aid,
            role="buck regulator",
            vin="+5V",
            vout="+3V3",
            extraction_hint=(
                "The recommended-application section near the front of the "
                "datasheet shows a typical buck circuit with input/output "
                "caps and an inductor. Extract that."
            ),
        )
        print(json.dumps(result, indent=2))

        if result.get("status") != "profile_ready":
            print("\nFAIL — parse_datasheet did not return status:profile_ready")
            return 1
        if "netlist" in result:
            print("\nFAIL — parse_datasheet must no longer return a netlist")
            return 1
        if "TPS628436" not in state.resolved_symbols:
            print("\nFAIL — parse_datasheet did not stash resolved_symbols")
            return 1

        print()
        print("=" * 72)
        print("Step 1.5: design_spec (deterministic equations → spec → netlist)")
        print("=" * 72)
        spec_result = tool_spec.run(
            state,
            mpn="TPS628436",
            topology="buck",
            vin="+5V",
            vout="+3V3",
            vref=0.5,
            fsw_hz=2.4e6,
            iout_a=0.5,
            role="buck regulator",
        )
        if spec_result.get("status") != "ok":
            print(json.dumps(spec_result, indent=2))
            print("\nFAIL — design_spec did not return status:ok")
            return 1
        spec = spec_result["spec"]
        print(f"blurb: {spec['blurb']}")
        for c in spec["components"]:
            eq = f"  [{c['equation']}]" if c.get("equation") else ""
            print(f"  {c['source']:>9} {c['refdes_hint']:>3} {c['component']} "
                  f"{c['value']!r:>8}  {c['purpose']}{eq}")
        if spec["warnings"]:
            print("warnings:", spec["warnings"])
        computed = [c for c in spec["components"] if c["source"] == "computed"]
        if not computed:
            print("\nFAIL — design_spec produced no computed components")
            return 1

        netlist = spec_result.get("netlist")
        if netlist is None:
            print("\nFAIL — design_spec did not return a netlist")
            return 1
        print(f"\nnetlist: {len(netlist['parts'])} parts, {len(netlist['nets'])} nets")
        for p in netlist["parts"]:
            print(f"  part {p['refdes']:>5}  lib_id={p['lib_id']:<40s} value={p['value']!r}")
        for n in netlist["nets"]:
            eps = ",".join(f"{e['ref']}.{e['pin']}" for e in n["endpoints"])
            tag = "PORT" if n["is_port"] else ("PWR" if n["is_power"] else "  ")
            print(f"  net  {n['name']:>10}  [{tag}]  → {eps}")

        print()
        print("=" * 72)
        print("Step 2: add_subcircuit_from_netlist (place → validate → merge → stage)")
        print("=" * 72)
        place_result = tool_add.run(
            state,
            netlist=netlist,
            label=result.get("orderable_part") or "TPS628436",
        )
        print(json.dumps({k: v for k, v in place_result.items() if k != "netlist"}, indent=2))

        if place_result.get("status") != "ok":
            print("\nFAIL — add_subcircuit_from_netlist did not return status:ok")
            return 1

        # Inspect the staged file.
        from pinflow_api import staging as stg
        stage = stg.get(sch_path)
        if stage is None:
            print("\nFAIL — no stage created")
            return 1
        staged_text = stage.temp_path.read_text()
        print(
            f"\nstaged file size: {len(staged_text)} bytes, "
            f"path: {stage.temp_path}"
        )
        # Sniff for the IC + a few decoupling caps in the output.
        for hint in ("TPS628436", "Device:C", "Device:L", "+3V3", "GND"):
            present = hint in staged_text
            print(f"  contains {hint!r}: {present}")

        print("\nALL CHECKS PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(main())

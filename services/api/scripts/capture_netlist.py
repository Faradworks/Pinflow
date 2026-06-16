"""Capture a prompt-derived Netlist once and freeze it as a reusable fixture.

The placer's golden corpus is reverse-derived from clean, hand-drawn
schematics — it does NOT reproduce the messier netlists the chat agent
actually emits, which is where layout quality breaks down. This tool freezes
one of those real, LLM-synthesized netlists to disk so the placer can be
iterated against it forever WITHOUT re-running the agent (an LLM call that
needs an API key + the gitignored datasheet PDF). Generate once, replay
forever — the committed JSON is the durable artifact.

It drives the SAME two agent tools the chat loop uses — `parse_datasheet`
(datasheet → cached profile + resolved KiCad symbol) then `design_spec`
(deterministic sizing → `netlist_synth` LLM call) — against a throwaway
conversation state, so the captured netlist is byte-for-byte what production
would place. No agent loop, no multi-turn prompt: just the netlist-first chain.

    cd services/api
    # warm profile cache (TPS62840 already parsed once) — no PDF needed:
    .venv/bin/python scripts/capture_netlist.py TPS62840 \
        --topology buck --vin +5V --vout +3V3 --vref 0.6 \
        --fsw-hz 2400000 --iout-a 0.5 --role "buck regulator"
    # cold — point at the datasheet PDF (it gets parsed by the LLM once):
    .venv/bin/python scripts/capture_netlist.py LM2731 --pdf ~/lm2731.pdf \
        --topology boost --vin +3V3 --vout +12V --name boost_lm2731

Writes tests/fixtures/generated/<name>.netlist.json (override with --out),
self-checks that it places + validates, and prints the manifest line to add
to tests/fixtures/generated_corpus.json. Needs ANTHROPIC_API_KEY (BYOK):
`design_spec` always makes one `netlist_synth` LLM call; a cold parse makes
a second. Exit 0 on a captured + validated netlist.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

from pinflow_api import llm  # noqa: E402
from pinflow_api.agent import attachments as _attachments  # noqa: E402
from pinflow_api.agent.state import ConversationState  # noqa: E402
from pinflow_api.agent.tools import design_spec, parse_datasheet  # noqa: E402
from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.placers import get_placer  # noqa: E402
from pinflow_api.emit.structural_diff import validate_placer_output  # noqa: E402

GENERATED = API_DIR / "tests" / "fixtures" / "generated"


def _slug(mpn: str) -> str:
    """'TPS62840DLCR' → 'tps62840dlcr' — a filesystem-safe fixture stem."""
    return re.sub(r"[^a-z0-9]+", "_", mpn.lower()).strip("_") or "netlist"


def _parse_bindings(pairs: list[str]) -> dict[str, str]:
    """`--port-binding VOUT=+4V5` (repeatable) → {'VOUT': '+4V5'}."""
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--port-binding must be NAME=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("mpn", help="Canonical MPN, e.g. 'TPS62840' (no reel suffix).")
    ap.add_argument("--topology", required=True,
                    help="buck | boost | buck_boost | ldo.")
    ap.add_argument("--vin", required=True, help="Input rail name, e.g. '+5V'.")
    ap.add_argument("--vout", required=True, help="Output rail name, e.g. '+3V3'.")
    ap.add_argument("--pdf", type=Path,
                    help="Datasheet PDF. Required on a cold cache; omit to reuse "
                         "a profile already parsed on this machine.")
    ap.add_argument("--variant-hint",
                    help="Package code or orderable part to pin the variant.")
    ap.add_argument("--vref", type=float,
                    help="Feedback reference voltage (V) — for a correct divider.")
    ap.add_argument("--fsw-hz", type=float, help="Switching frequency (Hz).")
    ap.add_argument("--iout-a", type=float, help="Max output current (A).")
    ap.add_argument("--role", help="Block role, e.g. 'buck regulator'.")
    ap.add_argument("--manufacturer", help="Optional manufacturer name.")
    ap.add_argument("--extraction-hint",
                    help="Optional prompt to bias the datasheet extraction.")
    ap.add_argument("--port-binding", action="append", metavar="NAME=VALUE",
                    default=[], help="Rename a default port net (repeatable).")
    ap.add_argument("--name",
                    help="Fixture stem (default: slug of MPN).")
    ap.add_argument("--out", type=Path,
                    help="Output path (default: tests/fixtures/generated/"
                         "<name>.netlist.json).")
    args = ap.parse_args()

    # Both the cold parse and design_spec's netlist_synth are LLM calls — fail
    # early and loudly rather than halfway through a paid round-trip.
    if not llm.available():
        print(f"capture needs an Anthropic API key (BYOK).\n{llm.NOT_CONFIGURED_MSG}",
              file=sys.stderr)
        return 2

    name = args.name or _slug(args.mpn)
    out = args.out or (GENERATED / f"{name}.netlist.json")
    bindings = _parse_bindings(args.port_binding)

    state = ConversationState(conversation_id=f"capture-{name}")

    # Step 1: parse_datasheet — datasheet/cache → resolved symbol. Register the
    # PDF as an attachment exactly as routes/agent.py would, then hand its id in.
    aid = ""
    if args.pdf:
        if not args.pdf.is_file():
            print(f"error: --pdf not found: {args.pdf}", file=sys.stderr)
            return 1
        ref = _attachments.save(
            state.conversation_id,
            filename=args.pdf.name,
            mime="application/pdf",
            data=args.pdf.read_bytes(),
        )
        state.attachments[ref.attachment_id] = ref
        aid = ref.attachment_id

    print(f"[1/2] parse_datasheet {args.mpn} ...", flush=True)
    pr = parse_datasheet.run(
        state,
        mpn=args.mpn,
        attachment_id=aid,
        variant_hint=args.variant_hint,
        manufacturer=args.manufacturer,
        extraction_hint=args.extraction_hint,
        role=args.role,
        vin=args.vin,
        vout=args.vout,
        port_bindings=bindings or None,
    )
    if pr.get("status") != "profile_ready":
        print(f"  parse_datasheet returned status={pr.get('status')!r}",
              file=sys.stderr)
        if pr.get("hint"):
            print(f"  hint: {pr['hint']}", file=sys.stderr)
        if pr.get("status") in ("needs_datasheet", "no_such_attachment") and not aid:
            print("  → no cached profile; pass --pdf <datasheet>.", file=sys.stderr)
        return 1
    print(f"  resolved {pr.get('lib_id')}  (source: {pr.get('symbol_source')})")

    # Step 2: design_spec — deterministic sizing + netlist_synth LLM call.
    print(f"[2/2] design_spec ({args.topology}) ...", flush=True)
    ds = design_spec.run(
        state,
        mpn=args.mpn,
        topology=args.topology,
        vin=args.vin,
        vout=args.vout,
        vref=args.vref,
        fsw_hz=args.fsw_hz,
        iout_a=args.iout_a,
        role=args.role,
        port_bindings=bindings or None,
    )
    if ds.get("status") != "ok":
        print(f"  design_spec returned status={ds.get('status')!r}",
              file=sys.stderr)
        for k in ("error", "hint"):
            if ds.get(k):
                print(f"  {k}: {ds[k]}", file=sys.stderr)
        return 1

    netlist_dict = ds["netlist"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(netlist_dict, indent=2) + "\n")
    print(f"\nwrote {out}")

    # Self-check: the whole point is that this netlist replays. Run the default
    # placer now so a missing-symbol / unplaceable capture fails here, loudly,
    # rather than silently as a broken fixture later.
    nl = Netlist.model_validate(netlist_dict)
    result = get_placer()(nl, title=name)
    vr = validate_placer_output(nl, result)
    print(f"  self-check: parts={len(nl.parts)} nets={len(nl.nets)}  "
          f"placed={len(result.placed_refs)}  validates={vr.ok}")
    if not vr.ok:
        for e in vr.errors:
            print(f"    - {e}", file=sys.stderr)

    # A non-bundled symbol's .kicad_sym is NOT inside the netlist JSON — the
    # fixture isn't self-contained without a sidecar lib. Flag it so replay
    # doesn't fail on a fresh checkout.
    rs = state.resolved_symbols.get(args.mpn, {})
    sidecar_note = ""
    if rs.get("symbol_source") and rs["symbol_source"] != "bundled":
        sidecar_note = (
            f'  ⚠ symbol source is {rs["symbol_source"]!r} (not bundled): add a '
            f'{out.stem}.symbols/ sidecar so the fixture replays on a clean '
            f"checkout — see tests/fixtures/README.md."
        )
        print(sidecar_note, file=sys.stderr)

    floor = round(max(0.0, _score_or_zero(result, nl) - 0.02), 2)
    print("\nAdd to tests/fixtures/generated_corpus.json → \"entries\":")
    entry = {
        "name": name,
        "netlist": f"generated/{out.name}",
        "source": "captured",
        "score_floor": floor,
        "note": f"{args.mpn} {args.topology}, vin={args.vin} vout={args.vout}.",
    }
    print(json.dumps(entry, indent=2))
    print(f"\nThen iterate:  .venv/bin/python scripts/eval_layout.py "
          f"--manifest tests/fixtures/generated_corpus.json --only {name} --render")
    return 0 if vr.ok else 4


def _score_or_zero(result, nl: Netlist) -> float:
    """Rubric total for the captured placement; 0.0 if scoring can't run.
    Seeds the suggested score_floor (measured minus jitter, the check_all
    convention) so the printed manifest line is paste-ready."""
    try:
        from pinflow_api.emit.rubric import score
        return score(result.sch_text, nl).total
    except Exception:  # noqa: BLE001
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())

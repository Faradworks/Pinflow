"""LLM refiner — pass 2 of the netlist→schematic pipeline.

`place()` (pass 1, `emit.netlist_to_sch`) produces a correct, connectivity-
verified, but column-shaped schematic — readable enough to review, not yet
laid out like a hand-drawn page. `refine()` is pass 2: it hands that result
— plus a high-fidelity render of it — to Claude and asks for a clean *full
re-layout*, expressed as a kicad-sch-api `build()` module (the codebase's
"generate code, not raw S-expr" decision; ksa validates lib_ids/pins at
runtime). The module is sandbox-executed via `_run_build`.

Every refined attempt is gated:
  - `structural_diff.diff_structure` — KiCad's own netlister must report the
    *same net topology* as pass 1. A re-layout that shorts or breaks a net
    is rejected, the error fed back, and the model retries.
  - ERC — the refined schematic must not raise *more* real errors than the
    baseline (subcircuit-context rules excepted).
  - Quality — the re-layout must be no *less* readable than pass 1, measured
    by diagonal-wire count and total wire length. The LLM places parts
    freely and `add_wire_between_pins` draws point-to-point, so a re-layout
    can be connectivity-correct yet uglier than the orthogonal baseline;
    this gate keeps "never worse" covering readability, not just correctness.

The deterministic pass-1 output is the floor: if no refined attempt passes
all three gates within `max_attempts`, `refine()` returns pass 1 unchanged.
The refiner can make the schematic prettier — never worse.
"""

from __future__ import annotations

import base64
import re
import subprocess
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pinflow_api import llm

from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.netlist_to_sch import PlacerResult
from pinflow_api.emit.render import render_schematic_bytes
from pinflow_api.emit.structural_diff import diff_structure
from pinflow_api.kicad_cli import run_erc
from pinflow_api.llm_emit import BuilderExecutionError
from pinflow_api.settings import settings

# ERC rules excluded from the "did the refiner make ERC worse?" comparison.
# Two groups: (a) inherent to a subcircuit drawn without board context — no
# upstream power source, a regulator output meeting a rail flag — and (b)
# environmental / cosmetic (off-grid endpoints, missing footprint links,
# library-symbol drift) which say nothing about whether the re-layout wired
# the circuit correctly. The connectivity gate (`diff_structure`) is the real
# topology check; this comparison only guards against new *wiring* defects
# such as a genuinely unconnected pin.
_ERC_IGNORE = frozenset({
    "power_pin_not_driven", "pin_not_driven", "pin_to_pin",
    "endpoint_off_grid", "lib_symbol_issues", "footprint_link_issues",
})

_REFINER_SYSTEM = textwrap.dedent(
    """\
    You re-lay-out a KiCad subcircuit for READABILITY. A deterministic placer
    already produced an electrically-correct version; you are shown a
    screenshot of it. Redraw the SAME circuit — same parts, same connections
    — laid out like a clean, hand-drawn schematic.

    Output a Python module with one function `def build() -> ksa.Schematic`.
    The only import allowed is `import kicad_sch_api as ksa`. Output ONLY the
    module text — no markdown fences, no prose.

    API — use ONLY these primitives, no other ksa method:
      sch = ksa.create_schematic("title")
      sch.components.add(lib_id=..., reference=..., value=...,
                         position=(x, y), footprint="", add_all_units=False)
      sch.add_wire_between_pins("U1", "1", "C1", "2")   # connect by pin number
      return sch
    Wires connect by PIN NUMBER (strings), never by coordinate — the library
    computes pin positions. Do NOT call sch.add_wire(), sch.add_label() or any
    other method.

    HARD RULE — connectivity is fixed and independently verified against the
    netlist below. Place every part; for every net, wire ALL of its endpoints
    together. A net with N endpoints needs N-1 add_wire_between_pins calls —
    chain them (ep0-ep1, ep1-ep2, …) so every endpoint ends up on one net.
    Output that changes which pins share a net is auto-rejected. Use each
    part's exact lib_id, reference, value and pin numbers from the netlist.

    POWER / GROUND nets — use power symbols, never long wires between far pins:
    - Add a power symbol with components.add(lib_id="power:GND" |
      "power:+3V3" | "power:+5V" | "power:VCC", reference a unique
      "#PWR1"/"#PWR2"/…, value="GND"/…, position=(x, y)). Power symbols have
      one pin, "1".
    - Wire each endpoint of a ground/rail net to a power symbol's pin "1".
      A multi-pin GND net may use several power:GND symbols (one near each
      pin) — every power:GND symbol is the same net.
    - For a named rail with no standard symbol, use lib_id="power:VCC" and set
      value to the net name.

    READABILITY — the whole point of this pass:
    - Signal flows left→right: input on the left, the IC central, output right.
    - Place each support part right next to the IC pin it serves.
    - power:+3V3 / power:+5V symbols sit ABOVE their pin; power:GND BELOW.
    - Decoupling / rail caps line up evenly along the rail.
    - A feedback divider is a vertical resistor stack.
    - Positions in mm, multiples of 2.54; Y grows downward. Spread parts
      10-30mm apart — nothing overlaps.

    You do NOT need to mark unused pins — no-connect markers are re-applied
    deterministically after your layout.
    """
)


@dataclass
class RefineReport:
    """Outcome of a `refine()` call — for the trace / debug surfaces."""

    refined: bool                       # True → result is the LLM re-layout
    attempts: int = 0
    notes: list[str] = field(default_factory=list)
    last_code: str | None = None        # most recent build() module emitted
    last_sch: str | None = None         # most recent serialized schematic
    last_error: str | None = None       # why the last attempt was rejected


def _strip_fences(code: str) -> str:
    """Drop a leading/trailing ``` fence if the model added one anyway."""
    if code.startswith("```"):
        nl = code.find("\n")
        code = code[nl + 1:] if nl != -1 else code[3:]
        if code.rstrip().endswith("```"):
            code = code.rstrip()[:-3]
    return code.strip()


# Subprocess runner: execute the model's build(), stamp the netlist's
# no-connect X markers onto the *freshly-built* schematic, then serialize.
# The stamp must run here, not after a reload — kicad-sch-api 0.5.x drops
# pin metadata on `load_schematic`, so `_place_no_connects` (which needs pin
# coordinates) sees zero pins on a reloaded component.
_RUNNER = textwrap.dedent(
    """
    # --- pinflow refiner runner ---
    import sys as _sys, json as _json
    from pinflow_api.builders._common import sch_to_string as _to_str
    from pinflow_api.emit.netlist import Netlist as _Netlist
    from pinflow_api.emit.netlist_to_sch import _place_no_connects as _stamp
    _sch = build()
    _nl = _Netlist.model_validate(_json.loads(_NL_JSON))
    _stamp(_sch, _nl, {p.refdes: (0.0, 0.0) for p in _nl.parts}, [])
    _sys.stdout.write(_to_str(_sch))
    """
)


def _run_build(
    code: str, netlist: Netlist, extra_lib_path: Path | None
) -> str:
    """Execute the model's build() module in a subprocess and return the
    serialized schematic, with no-connect markers stamped on. Raises
    `BuilderExecutionError` (carrying stderr + code) on any runtime failure."""
    preamble = ""
    if extra_lib_path is not None:
        preamble = (
            "import kicad_sch_api as _ksa\n"
            f"_ksa.get_symbol_cache().discover_libraries("
            f"{[str(extra_lib_path)]!r})\n"
        )
    wrapper = (
        preamble
        + f"_NL_JSON = {netlist.model_dump_json()!r}\n"
        + code
        + "\n\n"
        + _RUNNER
    )
    result = subprocess.run(
        [sys.executable, "-c", wrapper],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise BuilderExecutionError(
            f"refiner build() failed (exit {result.returncode}):\n"
            f"{result.stderr}",
            stderr=result.stderr, code=code,
        )
    return result.stdout


# A schematic wire: (wire (pts (xy x1 y1) (xy x2 y2)) ...). \s+ spans
# newlines, so the multi-line serialized form is matched too.
_WIRE_RE = re.compile(
    r"\(wire\s+\(pts\s+\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s+"
    r"\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)"
)
# Wire-length comparison tolerance — a re-layout within this factor of the
# baseline's total length counts as "no longer".
_LEN_TOL = 1.05


def _layout_quality(sch_text: str) -> tuple[int, float]:
    """Readability metric — `(diagonal-wire count, total wire length)`, lower
    is cleaner. A diagonal (non-orthogonal) wire is the strongest readability
    smell; total length is the tie-breaker. Placement *grouping* quality is
    not directly measurable here, so the gate that uses this only asserts the
    re-layout is *no worse* — it cannot positively reward better grouping."""
    diag = 0
    total = 0.0
    for x1, y1, x2, y2 in _WIRE_RE.findall(sch_text):
        dx = abs(float(x2) - float(x1))
        dy = abs(float(y2) - float(y1))
        if dx > 0.01 and dy > 0.01:
            diag += 1
        total += (dx * dx + dy * dy) ** 0.5
    return diag, total


def _erc_rule_counts(sch_text: str) -> Counter[str]:
    """Per-rule count of real (non-ignored) ERC errors; empty if ERC can't run."""
    try:
        rpt = run_erc(sch_text)
    except Exception:
        return Counter()
    return Counter(
        v.rule for v in rpt.violations
        if v.severity == "error" and v.rule not in _ERC_IGNORE
    )


def _user_block(netlist: Netlist, feedback: str | None) -> str:
    fb = (
        f"\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED — fix this and try again:\n"
        f"{feedback}\n"
        if feedback
        else ""
    )
    return textwrap.dedent(
        """\
        The screenshot above is a correct but cluttered placement of this
        subcircuit. Re-lay it out cleanly. The netlist below is the exact,
        immutable connectivity — place every part, connect every net.

        Netlist:
        """
    ) + netlist.model_dump_json(indent=2) + fb + (
        "\n\nWrite the build() module now. Output only the module text."
    )


def _ask_refiner(
    netlist: Netlist, png: bytes, feedback: str | None
) -> str:
    """One LLM round: returns the `build()` module text."""
    client = llm.make_client()
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=8192,
        system=_REFINER_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "Current deterministic layout (screenshot):"},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode("ascii"),
                }},
                {"type": "text", "text": _user_block(netlist, feedback)},
            ],
        }],
    )
    code = "".join(
        b.text for b in response.content
        if getattr(b, "type", None) == "text"
    ).strip()
    return _strip_fences(code)


def refine(
    netlist: Netlist,
    baseline: PlacerResult,
    *,
    extra_lib_path: Path | None = None,
    max_attempts: int = 3,
) -> tuple[PlacerResult, RefineReport]:
    """Pass-2 LLM re-layout of `baseline` (the deterministic `place()` output).

    Returns `(result, report)`. `result` is the refined `PlacerResult` when a
    re-layout passes the connectivity + ERC gate; otherwise it is `baseline`
    unchanged — the deterministic output is the floor, never worsened.

    `extra_lib_path` is registered with kicad-sch-api inside the build()
    sandbox (needed when the netlist uses project-local / lifted symbols).
    """
    report = RefineReport(refined=False)
    try:
        png = render_schematic_bytes(baseline.sch_text)
    except Exception as e:  # noqa: BLE001
        report.notes.append(f"could not render the baseline: {e}")
        return baseline, report

    base_erc = sum(_erc_rule_counts(baseline.sch_text).values())
    base_q = _layout_quality(baseline.sch_text)
    feedback: str | None = None

    for attempt in range(1, max_attempts + 1):
        report.attempts = attempt
        try:
            code = _ask_refiner(netlist, png, feedback)
        except Exception as e:  # noqa: BLE001
            report.notes.append(f"attempt {attempt}: LLM call failed — {e}")
            break
        report.last_code = code

        try:
            refined_text = _run_build(code, netlist, extra_lib_path)
        except BuilderExecutionError as e:
            feedback = f"Your build() raised an error:\n{e.stderr[-1500:]}"
            report.last_error = feedback
            report.notes.append(f"attempt {attempt}: build() failed to run")
            continue
        report.last_sch = refined_text

        vr = diff_structure(baseline, refined_text)
        if not vr.ok:
            detail = "; ".join(vr.errors)
            feedback = "Connectivity changed — rejected:\n" + "\n".join(
                vr.errors
            )
            report.last_error = feedback
            report.notes.append(
                f"attempt {attempt}: connectivity gate failed — {detail}"
            )
            continue

        ref_counts = _erc_rule_counts(refined_text)
        ref_erc = sum(ref_counts.values())
        if ref_erc > base_erc:
            detail = ", ".join(f"{r}×{n}" for r, n in ref_counts.items())
            feedback = (
                f"ERC wiring errors increased {base_erc}→{ref_erc} "
                f"({detail}) — a pin is unconnected. Wire every pin of every "
                "part; keep connectivity identical to the netlist."
            )
            report.last_error = feedback
            report.notes.append(
                f"attempt {attempt}: ERC worsened {base_erc}→{ref_erc} "
                f"({detail})"
            )
            continue

        ref_q = _layout_quality(refined_text)
        if ref_q[0] > base_q[0] or ref_q[1] > base_q[1] * _LEN_TOL:
            feedback = (
                f"The re-layout is not cleaner than the baseline — "
                f"diagonal wires {ref_q[0]} vs {base_q[0]}, total wire "
                f"length {ref_q[1]:.0f} vs {base_q[1]:.0f}mm. Every wire must "
                "be orthogonal: place the two pins of each connection on the "
                "SAME X (vertical wire) or SAME Y (horizontal wire), and keep "
                "parts compact so wires stay short."
            )
            report.last_error = feedback
            report.notes.append(
                f"attempt {attempt}: quality gate — diag "
                f"{ref_q[0]}/{base_q[0]}, len {ref_q[1]:.0f}/{base_q[1]:.0f}"
            )
            continue

        report.refined = True
        report.notes.append(f"attempt {attempt}: accepted")
        return (
            PlacerResult(
                sch_text=refined_text,
                issues=list(baseline.issues) + ["re-laid-out by LLM pass-2"],
                placed_refs={},   # baseline's coordinates no longer apply
                label_specs=[],
            ),
            report,
        )

    report.notes.append(
        "kept the deterministic layout — no refined attempt passed the gate"
    )
    return baseline, report

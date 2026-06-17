"""Dump a netlist as a JSON layout graph for the force-sim debug viewer.

A *debug* bridge — not part of the placement pipeline. It turns a `Netlist`
fixture into the node/edge graph that `dev/layout-sim/index.html` animates, so
the force-directed placement experiment can be watched and its force gains
tuned in a browser. The settled result is never fed back to KiCad from here;
the production path stays `emit.placers` -> S-expression -> clipboard.

Each node carries the part's *intrinsic* pin geometry at rotation 0 — local
offset and facing unit vector per pin — so the sim owns rotation (the discrete
re-orient step) itself. Pin facing is read from `pin.rotation` (which in KiCad
points *into* the body) negated, then Y-flipped to the screen frame, the same
transform `_pin_xy` applies to pin position.

    cd services/api
    .venv/bin/python scripts/dump_layout_graph.py buck_tps62840
    .venv/bin/python scripts/dump_layout_graph.py mcu_rp2040 --out ../../dev/layout-sim/graph.json
    .venv/bin/python scripts/dump_layout_graph.py tests/fixtures/golden/ams1117.netlist.json

`fixture` is a path, or a bare name resolved under tests/fixtures/generated/
then tests/fixtures/golden/ (`.netlist.json` suffix optional).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import kicad_sch_api as ksa

from pinflow_api.emit.netlist import (
    Netlist,
    is_ground_net_name,
    is_power_net_name,
)

_FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
_VIEWER = Path(__file__).resolve().parents[3] / "dev" / "layout-sim"
_PASSIVE_PREFIXES = ("Device:R", "Device:C", "Device:L", "Device:D", "Device:LED")


def _resolve_fixture(arg: str) -> Path:
    """Accept a path or a bare corpus name (generated/ then golden/). Raises
    FileNotFoundError (not sys.exit) so the live server can report it without
    dying."""
    p = Path(arg)
    if p.exists():
        return p
    stem = arg if arg.endswith(".netlist.json") else f"{arg}.netlist.json"
    for sub in ("generated", "golden"):
        cand = _FIX / sub / stem
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"fixture not found: {arg!r} (looked in {_FIX}/generated and /golden)")


def _axis_snap(dx: float, dy: float) -> tuple[float, float]:
    """Snap a facing vector to the nearest axis — wires are orthogonal."""
    if abs(dx) >= abs(dy):
        return (1.0 if dx > 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy > 0 else -1.0)


def _pin_geometry(comp) -> list[dict]:
    """Per-pin {num, ox, oy, fx, fy, type} in the screen frame (Y-down), at the
    component's native rotation 0. The sim applies node rotation on top."""
    out: list[dict] = []
    for p in comp.pins:
        rot = math.radians(float(p.rotation or 0.0))
        # lib outward = -(cos, sin); screen frame flips Y -> (-cos, +sin)
        fx, fy = _axis_snap(-math.cos(rot), math.sin(rot))
        out.append({
            "num": str(p.number),
            "ox": round(float(p.position.x), 3),
            "oy": round(-float(p.position.y), 3),  # lib Y-up -> screen Y-down
            "fx": fx,
            "fy": fy,
            "type": str(getattr(p, "pin_type", "")),
        })
    return out


def _synthetic_pins(pin_nums: list[str]) -> list[dict]:
    """Fallback geometry when a symbol won't resolve: lay the referenced pins
    evenly down the two sides of a box, facing outward. Keeps the viewer alive
    on parts the stock libraries don't carry."""
    out: list[dict] = []
    half = max(1, math.ceil(len(pin_nums) / 2))
    pitch = 2.54
    for i, num in enumerate(pin_nums):
        left = i < half
        row = i if left else i - half
        oy = (row - (half - 1) / 2) * pitch
        ox = -5.08 if left else 5.08
        out.append({"num": num, "ox": ox, "oy": oy,
                    "fx": -1.0 if left else 1.0, "fy": 0.0, "type": "unknown"})
    return out


def _node_for_part(part, refs_to_pins: dict[str, list[str]]) -> dict:
    pins: list[dict]
    lib_ok = True
    sch = ksa.create_schematic("dump")
    try:
        sch.components.add(lib_id=part.lib_id, reference=part.refdes,
                           value=part.value or part.refdes, position=(0.0, 0.0))
        pins = _pin_geometry(list(sch.components)[-1])
    except Exception:
        lib_ok = False
        pins = _synthetic_pins(refs_to_pins.get(part.refdes, []))

    xs = [p["ox"] for p in pins] or [0.0]
    ys = [p["oy"] for p in pins] or [0.0]
    # half-extents over pin span, padded by a grid step so bodies don't touch
    hx = (max(xs) - min(xs)) / 2 + 1.27
    hy = (max(ys) - min(ys)) / 2 + 1.27
    is_ic = len(pins) >= 4 and not part.lib_id.startswith(_PASSIVE_PREFIXES)
    return {
        "ref": part.refdes,
        "lib_id": part.lib_id,
        "value": part.value or part.refdes,
        "is_ic": is_ic,
        "symbol_resolved": lib_ok,
        "hx": round(hx, 3),
        "hy": round(hy, 3),
        "pins": pins,
    }


def _net_kind(net) -> str:
    if is_ground_net_name(net.name):
        return "ground"
    if net.is_power or is_power_net_name(net.name):
        return "power"
    return "signal"


def build_graph(netlist: Netlist, name: str) -> dict:
    refs_to_pins: dict[str, list[str]] = {}
    for net in netlist.nets:
        for ep in net.endpoints:
            refs_to_pins.setdefault(ep.ref, []).append(ep.pin)

    nodes = [_node_for_part(p, refs_to_pins) for p in netlist.parts]
    edges = [{
        "net": net.name,
        "kind": _net_kind(net),
        "is_port": net.is_port,
        "endpoints": [{"ref": ep.ref, "pin": ep.pin} for ep in net.endpoints],
    } for net in netlist.nets]
    return {"name": name, "nodes": nodes, "edges": edges}


# --- live tuner server ------------------------------------------------------
#
# `--serve` turns the viewer into a live gain-tuning console: the browser's
# sliders re-request `/api/trace?fixture=…&repel_aniso=…&…`, this handler runs
# the *real* `fdplace.trace_layout` with those gains, and the page re-animates.
# The single-source-of-truth rule holds — there is no JS physics; the server
# always runs the production `fdcore.simulate`.

def _list_fixtures() -> list[str]:
    names: set[str] = set()
    for sub in ("generated", "golden"):
        d = _FIX / sub
        if d.is_dir():
            for p in d.glob("*.netlist.json"):
                names.add(p.name[: -len(".netlist.json")])
    return sorted(names)


def _trace_payload(query: dict[str, list[str]]) -> dict:
    """Build a `SimConfig` from query params (any DEFAULT_GAINS key, plus
    `iters`/`margin`) and return the viewer trace for `?fixture=`."""
    from pinflow_api.emit import fdcore
    from pinflow_api.emit.placers.fdplace import trace_layout

    fixture = (query.get("fixture") or [None])[0]
    if not fixture:
        raise ValueError("missing ?fixture=")
    path = _resolve_fixture(fixture)
    netlist = Netlist.model_validate(json.loads(path.read_text()))

    gains = dict(fdcore.DEFAULT_GAINS)
    for key in fdcore.DEFAULT_GAINS:
        if key in query:
            gains[key] = float(query[key][0])
    base = fdcore.SimConfig()
    iters = int(query["iters"][0]) if "iters" in query else base.iters
    margin = float(query["margin"][0]) if "margin" in query else base.margin
    cfg = fdcore.SimConfig(gains=gains, iters=iters, margin=margin)

    name = path.stem.replace(".netlist", "")
    return trace_layout(netlist, title=name, cfg=cfg)


def _serve(port: int) -> None:
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    from pinflow_api.emit import fdcore

    viewer = str(_VIEWER)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=viewer, **kw)

        def _send_json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/api/defaults":
                base = fdcore.SimConfig()
                return self._send_json(200, {
                    "gains": dict(fdcore.DEFAULT_GAINS),
                    "iters": base.iters,
                    "margin": base.margin,
                    "fixtures": _list_fixtures(),
                })
            if u.path == "/api/trace":
                try:
                    return self._send_json(200,
                                           _trace_payload(parse_qs(u.query)))
                except Exception as e:  # noqa: BLE001
                    return self._send_json(400,
                                           {"error": f"{type(e).__name__}: {e}"})
            return super().do_GET()

        def log_message(self, *a):  # quiet the per-request access log
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"layout-sim live tuner → http://127.0.0.1:{port}/  (Ctrl-C to stop)",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fixture", nargs="?",
                    help="path or bare corpus name (e.g. buck_tps62840)")
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    ap.add_argument("--trace", action="store_true",
                    help="run fdplace's force sim and dump {graph, frames, "
                         "snapped} for dev/layout-sim/index.html to animate")
    ap.add_argument("--serve", action="store_true",
                    help="serve dev/layout-sim/ with a live gain-tuning API "
                         "(sliders re-run the real sim); ignores fixture/--out")
    ap.add_argument("--port", type=int, default=8777,
                    help="port for --serve (default 8777)")
    args = ap.parse_args()

    if args.serve:
        _serve(args.port)
        return

    if not args.fixture:
        ap.error("fixture is required (or pass --serve)")

    try:
        path = _resolve_fixture(args.fixture)
    except FileNotFoundError as e:
        sys.exit(str(e))
    netlist = Netlist.model_validate(json.loads(path.read_text()))
    errs = netlist.validate_self()
    if errs:
        print(f"warning: netlist has {len(errs)} structural issue(s):", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)

    name = path.stem.replace(".netlist", "")
    if args.trace:
        from pinflow_api.emit.placers.fdplace import trace_layout
        payload = trace_layout(netlist, title=name)
        text = json.dumps(payload)  # compact — frames dominate the size
        summary = (f"traced {len(payload['graph']['nodes'])} nodes, "
                   f"{len(payload['frames'])} frames")
    else:
        graph = build_graph(netlist, name=name)
        unresolved = [n["ref"] for n in graph["nodes"] if not n["symbol_resolved"]]
        if unresolved:
            print(f"note: synthetic pins used for unresolved symbols: "
                  f"{', '.join(unresolved)}", file=sys.stderr)
        text = json.dumps(graph, indent=2)
        summary = f"wrote {len(graph['nodes'])} nodes, {len(graph['edges'])} nets"

    if args.out:
        Path(args.out).write_text(text)
        print(f"{summary} -> {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()

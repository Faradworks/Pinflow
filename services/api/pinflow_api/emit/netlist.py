"""Netlist intermediate format for the replicate / generate pipelines.

A flat, position-free description of a subcircuit: parts + nets. Produced by
`extract_subgraph` (from the active schematic's design graph) or by an LLM
(future, for the netlist-first generate path). Consumed by `emit.netlist_to_sch`
which turns it into a placed `(kicad_sch ...)` snippet.

Lives in `emit/` because it's an intermediate of the placement pipeline — not
a domain model. The richer `graph.models.DesignGraph` is the read-side model
that's joined with MPN profiles for digesting; this Netlist is the write-side
model the placer consumes.
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator


class NetlistPart(BaseModel):
    """A part placed in the subcircuit. No position — the placer assigns it."""

    refdes: str
    lib_id: str                # "Device:R", "Regulator_Switching:TPS628436DRL"
    value: str = ""
    footprint: str = ""
    mpn: str | None = None     # carried for digest enrichment after stage
    # design_spec-baked LCSC search hints (consumed by resolve_parts). Optional
    # and ignored by the placer; round-trips through pending/staged netlists.
    search_query: str | None = None   # keyword-search string for the catalogue
    min_voltage: float | None = None  # derating floor (V) for cap/inductor picks
    no_connect_pins: list[str] = []   # pins intentionally unconnected (X marker)


class NetlistEndpoint(BaseModel):
    ref: str
    pin: str                   # kicad pin number, kept as string


class NetlistNet(BaseModel):
    name: str
    is_power: bool = False     # POWER or GROUND in the source graph
    voltage: float | None = None
    endpoints: list[NetlistEndpoint] = []
    is_port: bool = False      # boundary net exposed for `port_bindings` rebind


class Netlist(BaseModel):
    parts: list[NetlistPart] = []
    nets: list[NetlistNet] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce_connectivity(cls, data):
        """Accept the two connectivity shapes the LLM emits, normalizing both
        to the canonical `nets[].endpoints[{ref, pin}]`:

        1. endpoints with `refdes` instead of `ref` (parts use `refdes`, so the
           model conflates them) — aliased to `ref`.
        2. connectivity expressed as `parts[].pins[{pin, net}]` instead of on
           the nets — lifted into the matching net's endpoints (creating the
           net if absent). Without this, pydantic silently drops the extra
           `pins` field and the placer wires nothing — a schematic of floating
           parts that still reports success.

        Idempotent on already-canonical input (extract_subgraph / design_spec
        output passes through unchanged).
        """
        if not isinstance(data, dict):
            return data

        net_by_name: dict[str, dict] = {}
        order: list[str] = []

        def _net(name: str) -> dict:
            if name not in net_by_name:
                net_by_name[name] = {"name": name, "endpoints": []}
                order.append(name)
            return net_by_name[name]

        for raw in data.get("nets") or []:
            n = {"name": raw} if isinstance(raw, str) else dict(raw or {})
            name = n.get("name")
            if name is None:
                continue
            eps = []
            for ep in n.get("endpoints") or []:
                if isinstance(ep, dict) and "ref" not in ep and "refdes" in ep:
                    ep = {**ep, "ref": ep["refdes"]}
                eps.append(ep)
            existing = _net(name)
            existing.update({k: v for k, v in n.items() if k != "endpoints"})
            existing["endpoints"].extend(eps)

        cleaned_parts = []
        for raw in data.get("parts") or []:
            if not isinstance(raw, dict):
                cleaned_parts.append(raw)
                continue
            p = dict(raw)
            pins = p.pop("pins", None)  # non-canonical; lift onto nets
            if pins:
                for pin in pins:
                    if not isinstance(pin, dict):
                        continue
                    net_name, pin_num = pin.get("net"), pin.get("pin")
                    if net_name is None or pin_num is None:
                        continue
                    _net(net_name)["endpoints"].append(
                        {"ref": p.get("refdes"), "pin": pin_num}
                    )
            cleaned_parts.append(p)

        data = dict(data)
        data["parts"] = cleaned_parts
        data["nets"] = [net_by_name[name] for name in order]
        return data

    def with_port_bindings(self, bindings: dict[str, str]) -> "Netlist":
        """Return a copy with port nets renamed per `bindings`.

        Bindings only rename net names; endpoint refdeses stay put. Non-port
        nets are not renamed even if their name appears in `bindings` — that
        would silently re-wire internal connectivity, which is not what
        `port_bindings` means.
        """
        if not bindings:
            return self.model_copy(deep=True)
        renamed_nets = []
        for net in self.nets:
            if net.is_port and net.name in bindings:
                renamed_nets.append(net.model_copy(update={"name": bindings[net.name]}))
            else:
                renamed_nets.append(net.model_copy(deep=True))
        return Netlist(parts=[p.model_copy(deep=True) for p in self.parts], nets=renamed_nets)

    def ports(self) -> list[NetlistNet]:
        return [n for n in self.nets if n.is_port]

    def validate_self(self) -> list[str]:
        """Structural syntax-only validation. No symbol-library lookups.

        Returns a list of error strings; empty list means OK. The placer
        treats a non-empty list as fatal.
        """
        errors: list[str] = []

        refs_seen: set[str] = set()
        for p in self.parts:
            if p.refdes in refs_seen:
                errors.append(f"duplicate refdes in parts: {p.refdes}")
            refs_seen.add(p.refdes)
            if not p.lib_id:
                errors.append(f"part {p.refdes} has empty lib_id")

        names_seen: set[str] = set()
        for net in self.nets:
            if net.name in names_seen:
                errors.append(f"duplicate net name: {net.name}")
            names_seen.add(net.name)
            if not net.is_port and len(net.endpoints) == 0:
                errors.append(f"internal net {net.name!r} has no endpoints")
            # 1-endpoint non-port nets are degenerate but harmless — the
            # placer still drops a label there (it just doesn't connect to
            # anything). The fixture pipeline produces them when a pin's
            # only neighbor was outside the BFS boundary; not worth failing
            # the whole netlist over.
            for ep in net.endpoints:
                if ep.ref not in refs_seen:
                    errors.append(
                        f"net {net.name!r} references unknown refdes {ep.ref!r}"
                    )

        return errors

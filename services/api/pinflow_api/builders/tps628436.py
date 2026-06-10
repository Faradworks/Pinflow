"""TPS628436DRL minimal buck regulator subcircuit.

Substituted for TPS62840 because exact TPS62840 is not in KiCad 10's bundled
symbol libraries. TPS628436 is the closest sister part (TPS628xx ultra-low-Iq
buck family). Swap when the real datasheet ingestion lands.

Pin map (from KiCad sym):
  1 GND   2 VIN   3 VSET   4 EN   5 SW   6 VOS
"""

import kicad_sch_api as ksa

from ._common import sch_to_string

LIB_ID = "Regulator_Switching:TPS628436DRL"
CHIP_REF = "U1"
CHIP_POS = (150.0, 100.0)


def build() -> str:
    sch = ksa.create_schematic("TPS628436DRL buck regulator")

    sch.components.add(
        lib_id=LIB_ID,
        reference=CHIP_REF,
        value="TPS628436DRL",
        position=CHIP_POS,
        footprint="Package_SON:Texas_DRL-6_1.6x1.6mm_P0.5mm",
    )

    # VIN bulk cap (10uF, pin 2 → GND)
    sch.components.add(
        lib_id="Device:C",
        reference="C1",
        value="10uF",
        position=(120.0, 100.0),
        footprint="Capacitor_SMD:C_0603_1608Metric",
    )
    sch.add_wire_between_pins(CHIP_REF, "2", "C1", "1")

    # Output inductor (2.2uH on SW pin)
    sch.components.add(
        lib_id="Device:L",
        reference="L1",
        value="2.2uH",
        position=(180.0, 95.0),
        footprint="Inductor_SMD:L_0805_2012Metric",
    )
    sch.add_wire_between_pins(CHIP_REF, "5", "L1", "1")

    # VOUT cap (22uF)
    sch.components.add(
        lib_id="Device:C",
        reference="C2",
        value="22uF",
        position=(200.0, 100.0),
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )

    # Feedback divider (R1 high-side, R2 low-side) for fixed Vout via VSET pin 3
    sch.components.add(
        lib_id="Device:R",
        reference="R1",
        value="100k",
        position=(170.0, 110.0),
        footprint="Resistor_SMD:R_0402_1005Metric",
    )
    sch.components.add(
        lib_id="Device:R",
        reference="R2",
        value="100k",
        position=(170.0, 120.0),
        footprint="Resistor_SMD:R_0402_1005Metric",
    )
    sch.add_wire_between_pins(CHIP_REF, "3", "R1", "1")

    # EN pulled to VIN for always-on default
    sch.components.add(
        lib_id="Device:R",
        reference="R3",
        value="100k",
        position=(130.0, 90.0),
        footprint="Resistor_SMD:R_0402_1005Metric",
    )
    sch.add_wire_between_pins(CHIP_REF, "4", "R3", "1")

    # Net labels
    sch.add_label("VIN", position=(120.0, 95.0))
    sch.add_label("VOUT", position=(200.0, 95.0))
    sch.add_label("GND", position=(150.0, 130.0))
    sch.add_label("EN", position=(130.0, 85.0))

    return sch_to_string(sch)


if __name__ == "__main__":
    print(build()[:500])

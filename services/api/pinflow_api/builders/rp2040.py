"""RP2040 minimal subcircuit: chip + IOVDD/DVDD decoupling + crystal + RUN pull-up.

Topology is intentionally a useful-but-not-comprehensive minimum so the milestone
demo shows a real working subcircuit in eeschema. Refinement is for later.
"""

import kicad_sch_api as ksa

from ._common import sch_to_string

LIB_ID = "MCU_RaspberryPi:RP2040"
CHIP_REF = "U1"
CHIP_POS = (150.0, 100.0)

# IOVDD pin numbers per KiCad symbol
IOVDD_PINS = ["1", "10", "22", "33", "42", "49"]
DVDD_PINS = ["23", "50"]
GND_PIN = "57"  # thermal pad / GND on RP2040 KiCad symbol


def build() -> str:
    sch = ksa.create_schematic("RP2040 minimal subcircuit")

    # Main chip — RP2040's KiCad symbol is split across 2 units; place both.
    sch.components.add(
        lib_id=LIB_ID,
        reference=CHIP_REF,
        value="RP2040",
        position=CHIP_POS,
        footprint="Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm",
        add_all_units=True,
        unit_spacing=80.0,
    )

    # 100nF decoupling for each IOVDD pin (one per pin, laid out left of chip)
    for i, pin in enumerate(IOVDD_PINS):
        cap_ref = f"C{i + 1}"
        cap_y = 80.0 + i * 5.0
        sch.components.add(
            lib_id="Device:C",
            reference=cap_ref,
            value="100nF",
            position=(110.0, cap_y),
            footprint="Capacitor_SMD:C_0402_1005Metric",
        )
        # Wire cap pin 1 to chip's IOVDD pin
        sch.add_wire_between_pins(CHIP_REF, pin, cap_ref, "1")

    # DVDD decoupling
    for i, pin in enumerate(DVDD_PINS):
        cap_ref = f"C{len(IOVDD_PINS) + i + 1}"
        cap_y = 115.0 + i * 5.0
        sch.components.add(
            lib_id="Device:C",
            reference=cap_ref,
            value="100nF",
            position=(110.0, cap_y),
            footprint="Capacitor_SMD:C_0402_1005Metric",
        )
        sch.add_wire_between_pins(CHIP_REF, pin, cap_ref, "1")

    # 10uF bulk
    sch.components.add(
        lib_id="Device:C",
        reference="C9",
        value="10uF",
        position=(95.0, 95.0),
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )

    # RUN pull-up (10k)
    sch.components.add(
        lib_id="Device:R",
        reference="R1",
        value="10k",
        position=(180.0, 80.0),
        footprint="Resistor_SMD:R_0402_1005Metric",
    )
    sch.add_wire_between_pins(CHIP_REF, "26", "R1", "2")

    # 12 MHz crystal
    sch.components.add(
        lib_id="Device:Crystal_GND24",
        reference="Y1",
        value="12MHz",
        position=(190.0, 100.0),
        footprint="Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm",
    )

    # Crystal load caps
    for i, (ref, pos_y) in enumerate([("C10", 95.0), ("C11", 105.0)]):
        sch.components.add(
            lib_id="Device:C",
            reference=ref,
            value="27pF",
            position=(200.0, pos_y),
            footprint="Capacitor_SMD:C_0402_1005Metric",
        )

    # Net labels (placed at component positions; layout is approximate)
    sch.add_label("+3V3", position=(110.0, 75.0))
    sch.add_label("GND", position=(110.0, 130.0))

    return sch_to_string(sch)


if __name__ == "__main__":
    print(build()[:500])

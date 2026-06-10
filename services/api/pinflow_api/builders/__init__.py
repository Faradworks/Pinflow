"""Per-chip subcircuit builders. Each module exports a `build() -> str`."""

from . import rp2040, tps628436

CHIPS = {
    "rp2040": {
        "id": "rp2040",
        "name": "RP2040",
        "package": "QFN-56",
        "blurb": "Dual Cortex-M0+ MCU, 264KB SRAM",
        "lib_id": "MCU_RaspberryPi:RP2040",
        "module": rp2040,
    },
    "tps628436": {
        "id": "tps628436",
        "name": "TPS628436DRL",
        "package": "SOT-563",
        "blurb": "Adjustable buck regulator, 1A, ultra-low Iq",
        "lib_id": "Regulator_Switching:TPS628436DRL",
        "module": tps628436,
    },
}


def list_chips():
    return [
        {"id": c["id"], "name": c["name"], "package": c["package"], "blurb": c["blurb"]}
        for c in CHIPS.values()
    ]


def build_subcircuit(chip_id: str) -> str:
    if chip_id not in CHIPS:
        raise KeyError(chip_id)
    return CHIPS[chip_id]["module"].build()


def lib_id_for(chip_id: str) -> str:
    if chip_id not in CHIPS:
        raise KeyError(chip_id)
    return CHIPS[chip_id]["lib_id"]

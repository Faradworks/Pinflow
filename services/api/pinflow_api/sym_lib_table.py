"""Read/patch a project's `sym-lib-table` to register Pinflow's symbol library."""

from __future__ import annotations

import re
from pathlib import Path

PINFLOW_LIB_NAME = "pinflow"
PINFLOW_URI = "${KIPRJMOD}/pinflow.kicad_sym"
_DEFAULT_TABLE = "(sym_lib_table\n\t(version 7)\n)\n"


def ensure_pinflow_entry(table_path: Path) -> str:
    """Idempotently add a `(lib (name "pinflow") ...)` entry to the project's
    sym-lib-table. Creates the file if missing. Returns "added" or "already_present".
    """
    text = table_path.read_text() if table_path.is_file() else _DEFAULT_TABLE

    if _has_pinflow_entry(text):
        return "already_present"

    new_entry = (
        f'\t(lib (name "{PINFLOW_LIB_NAME}") (type "KiCad") '
        f'(uri "{PINFLOW_URI}") (options "") (descr "Pinflow generated symbols"))'
    )
    # Insert before the final `)` of `(sym_lib_table ...)`.
    last_close = text.rfind(")")
    if last_close < 0:
        # malformed; rewrite from scratch
        text = _DEFAULT_TABLE
        last_close = text.rfind(")")
    updated = text[:last_close].rstrip() + "\n" + new_entry + "\n" + text[last_close:]
    table_path.write_text(updated)
    return "added"


def _has_pinflow_entry(text: str) -> bool:
    # match (lib (name "pinflow") ...) or unquoted (lib (name pinflow) ...)
    return bool(re.search(rf'\(lib\s+\(name\s+"?{re.escape(PINFLOW_LIB_NAME)}"?\s*\)', text))

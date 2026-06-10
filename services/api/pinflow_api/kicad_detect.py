"""Detect the KiCad project currently open on this machine.

Strategy, in order:
  1. IPC API (KiCad 9+, requires `api.enable_server: true` in kicad_common.json) →
     authoritative project name and active schematic filename. Schematic filename
     is the only doc identifier the IPC currently returns; built-in path variables
     like ${KIPRJMOD} are NOT expanded by the API as of KiCad 10.
  2. lsof DIR scan on KiCad processes → if IPC isn't reachable, or it is but no
     schematic is open (eeschema not running), find the loaded project by looking
     for `.kicad_pro` files inside directories KiCad has open. This recovers the
     common case where the project manager has a project loaded but eeschema is
     closed, or where the IPC socket file has been removed (e.g. /tmp cleanup)
     while KiCad keeps running.

When the cloud lift happens, this module stays in a *local agent* — the cloud
server obviously cannot poll the user's KiCad.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


class KicadProject(BaseModel):
    name: str
    schematic: str  # active schematic filename (basename, no directory)
    path: Optional[str] = None  # full path to <name>.kicad_pro if found


def detect() -> Optional[KicadProject]:
    name, schematic = _ipc_active()
    if name is not None:
        pro_path = _find_project_pro(name)
        return KicadProject(
            name=name,
            schematic=schematic or _root_schematic_basename(pro_path, name),
            path=str(pro_path) if pro_path else None,
        )

    # IPC unreachable or no schematic doc open → fall back to scanning KiCad's
    # open directories for any .kicad_pro file.
    pro_path = _scan_kicad_processes_for_pro()
    if pro_path is None:
        return None
    return KicadProject(
        name=pro_path.stem,
        schematic=_root_schematic_basename(pro_path, pro_path.stem),
        path=str(pro_path),
    )


def _root_schematic_basename(pro_path: Optional[Path], project_name: str) -> str:
    """Guess the root schematic basename by KiCad convention (`<name>.kicad_sch`).

    Used when IPC didn't tell us which schematic is active. Only returns the
    basename if the file actually exists next to the .kicad_pro; otherwise "".
    """
    if pro_path is None:
        return ""
    candidate = pro_path.parent / f"{project_name}.kicad_sch"
    return candidate.name if candidate.is_file() else ""


def _ipc_active() -> tuple[Optional[str], Optional[str]]:
    """Returns (project_name, schematic_filename) via the IPC API, or (None, None)."""
    try:
        from kipy import KiCad
        from kipy.proto.common.types import base_types_pb2 as bt
    except ImportError:
        return None, None

    try:
        k = KiCad(timeout_ms=2000)
        k.ping()
    except Exception:
        return None, None

    try:
        docs = k.get_open_documents(bt.DOCTYPE_SCHEMATIC)
    except Exception:
        return None, None

    if not docs:
        return None, None

    doc = docs[0]
    schematic = doc.board_filename or ""
    try:
        proj = k.get_project(doc)
        name = proj.expand_text_variables("${PROJECTNAME}")
    except Exception:
        name = ""
    if not name and schematic:
        name = schematic.rsplit(".", 1)[0]
    return (name or None), (schematic or None)


def _find_project_pro(project_name: str) -> Optional[Path]:
    target_basename = f"{project_name}.kicad_pro"
    skip_prefixes = (
        "/Applications",
        "/Library",
        "/private",
        "/usr",
        "/System",
        "/opt",
        "/dev",
        "/cores",
    )
    for pid in _kicad_pids():
        for d in _open_dirs(pid, skip_prefixes):
            candidate = d / target_basename
            if candidate.is_file():
                return candidate
    return None


def _kicad_pids() -> list[int]:
    # Match the project manager and every editor process (eeschema, pcbnew,
    # kicad-cli, …). The DIR scan fallback may need any of them — the project
    # manager often has the project dir open, but eeschema does too when active.
    pids: list[int] = []
    for pattern in ("kicad", "eeschema", "pcbnew"):
        try:
            out = subprocess.run(
                ["pgrep", "-x", pattern],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            continue
        pids.extend(int(p) for p in out.stdout.split() if p.isdigit())
    # Dedup while preserving order.
    seen: set[int] = set()
    unique: list[int] = []
    for p in pids:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _scan_kicad_processes_for_pro() -> Optional[Path]:
    """Find a `.kicad_pro` by scanning every KiCad process's open directories.

    Used when IPC is unreachable. Returns the first `.kicad_pro` found inside any
    user-writable directory open by a KiCad-family process. If multiple are found
    (unusual — would mean the user has more than one project loaded), the shortest
    path wins, which biases toward the actual project dir over nested subfolders.
    """
    skip_prefixes = (
        "/Applications",
        "/Library",
        "/private",
        "/usr",
        "/System",
        "/opt",
        "/dev",
        "/cores",
    )
    candidates: list[Path] = []
    for pid in _kicad_pids():
        for d in _open_dirs(pid, skip_prefixes):
            try:
                for entry in d.iterdir():
                    if entry.suffix == ".kicad_pro" and entry.is_file():
                        candidates.append(entry)
            except (PermissionError, OSError):
                continue
    if not candidates:
        return None
    # Prefer the shortest path so a project dir beats a backups/ subdir if both show up.
    candidates.sort(key=lambda p: len(str(p)))
    return candidates[0]


def _open_dirs(pid: int, skip_prefixes: tuple[str, ...]) -> list[Path]:
    try:
        out = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except Exception:
        return []
    dirs: list[Path] = []
    seen: set[str] = set()
    for line in out.stdout.splitlines():
        parts = line.split(maxsplit=8)
        if len(parts) < 9 or parts[4] != "DIR":
            continue
        path = parts[8]
        if not path.startswith("/") or path.startswith(skip_prefixes):
            continue
        if path in seen:
            continue
        seen.add(path)
        dirs.append(Path(path))
    return dirs

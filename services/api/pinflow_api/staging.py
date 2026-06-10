"""Schematic staging layer.

A snapshot/edit/commit/discard cycle on top of KiCad schematic files. The agent
mutates a working copy; the user's actual .kicad_sch is untouched until commit.

State lives in memory + a spill file under the OS temp dir, keyed by the
absolute path of the real schematic. Restarts drop uncommitted stages by design
— the temp file is just so `git diff --no-index` has something to point at.

Local-only: stays in the per-machine agent post cloud-lift (the user's
filesystem isn't reachable from a hosted backend).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


class StaleStageError(RuntimeError):
    """Real file mtime advanced after stage — user likely saved in KiCad."""


class MalformedSchematicError(RuntimeError):
    """Staged working copy is not a parseable `.kicad_sch` — commit refused
    so a malformed file can't reach (and brick) the user's real project."""


@dataclass
class StagedSchematic:
    schematic_path: Path  # absolute, canonical
    working_copy: str
    real_mtime_at_stage: float
    temp_path: Path  # spill file (parent dir is the tempdir we own)
    # What this stage's edits touched, recorded by the editing tools so the
    # viewer can highlight exactly the work under review (operation-driven, not
    # a byte-diff against disk — that would also flag resolve_parts metadata
    # backfill and ksa round-trip churn). Preview-only: never written to
    # working_copy/temp_path, never committed.
    #   - block_regions: content bboxes (mm, page coords) of whole new blocks
    #   - changed_refs: refdes of individual components an edit touched
    block_regions: list[tuple] = field(default_factory=list)
    changed_refs: set = field(default_factory=set)
    # Memoized highlighted display copy: (cache_key, preview_text). The viewer
    # polls active-project on mount + every focus, so we avoid re-parsing the
    # schematic on every poll when nothing changed.
    _preview: Optional[tuple] = None

    def is_stale(self) -> bool:
        try:
            return self.schematic_path.stat().st_mtime > self.real_mtime_at_stage + 1e-6
        except OSError:
            return False


@dataclass
class CommitResult:
    file_written: bool


_stages: dict[str, StagedSchematic] = {}


def _key(p: Path) -> str:
    return str(p.resolve())


def get(schematic_path: Path) -> Optional[StagedSchematic]:
    return _stages.get(_key(schematic_path))


def stage(schematic_path: Path) -> StagedSchematic:
    """Snapshot the real file. Idempotent — returns existing stage if any."""
    existing = get(schematic_path)
    if existing is not None:
        return existing

    real = schematic_path.resolve()
    if not real.is_file():
        raise FileNotFoundError(f"schematic file not found: {real}")

    source = real.read_text(encoding="utf-8")
    mtime = real.stat().st_mtime
    spill_dir = Path(tempfile.mkdtemp(prefix="pinflow-stage-"))
    spill_file = spill_dir / real.name
    spill_file.write_text(source, encoding="utf-8")

    s = StagedSchematic(
        schematic_path=real,
        working_copy=source,
        real_mtime_at_stage=mtime,
        temp_path=spill_file,
    )
    _stages[_key(real)] = s
    return s


def update(schematic_path: Path, source: str) -> StagedSchematic:
    """Replace working copy. Auto-stages if no stage exists."""
    s = stage(schematic_path)
    s.working_copy = source
    s.temp_path.write_text(source, encoding="utf-8")
    s._preview = None  # working copy changed — drop the memoized preview
    return s


def add_block_region(schematic_path: Path, rect: tuple) -> None:
    """Record a whole-new-block's content bbox (mm, page coords) so the viewer
    outlines it as a unit. No-op if no stage exists. Preview-only metadata."""
    s = get(schematic_path)
    if s is None:
        return
    s.block_regions.append(tuple(rect))
    s._preview = None  # regions changed — drop the memoized preview


def add_changed_refs(schematic_path: Path, refs: Iterable[str]) -> None:
    """Record component refdes an edit touched so the viewer outlines them
    individually (unless they fall inside a recorded block region). No-op if no
    stage exists. Preview-only metadata."""
    s = get(schematic_path)
    if s is None:
        return
    new = {str(r) for r in refs if r}
    if not new or new <= s.changed_refs:
        return
    s.changed_refs |= new
    s._preview = None  # touched set changed — drop the memoized preview


def preview_source(schematic_path: Path) -> Optional[str]:
    """Working copy with preview-only highlight rectangles around new/changed
    content, for the viewer. None if no stage. Falls back to the plain working
    copy on any failure — the preview must never break the viewer's source.

    Highlights are operation-driven (recorded block regions + touched refdes),
    so this never re-reads or diffs the on-disk file. Memoized on (working-copy
    identity, block regions, changed refs): the viewer re-polls on every window
    focus, and re-parsing the schematic each time would be wasteful when
    nothing changed.
    """
    s = get(schematic_path)
    if s is None:
        return None

    cache_key = (
        hash(s.working_copy),
        tuple(s.block_regions),
        tuple(sorted(s.changed_refs)),
    )
    if s._preview is not None and s._preview[0] == cache_key:
        return s._preview[1]

    # Local import keeps staging's dependency direction one-way (staging is a
    # low-level primitive; emit/highlight depends on ksa + bbox), mirroring the
    # function-local kicad_cli import in `commit`.
    from pinflow_api.emit import highlight

    preview = highlight.build_preview(
        s.working_copy,
        block_regions=list(s.block_regions),
        changed_refs=s.changed_refs,
    )
    s._preview = (cache_key, preview)
    return preview


def discard(schematic_path: Path) -> bool:
    key = _key(schematic_path)
    s = _stages.pop(key, None)
    if s is None:
        return False
    shutil.rmtree(s.temp_path.parent, ignore_errors=True)
    return True


def commit(
    schematic_path: Path,
    *,
    force: bool = False,
) -> CommitResult:
    """Atomically write the working copy back to the real .kicad_sch file.

    Just a file write — no git. The user's project may not be a git repo, and
    we don't want to inject commits into their history. Audit trail (if any)
    is the user's own concern via KiCad/their VCS.

    Raises StaleStageError if the real file changed under us unless `force`.
    Raises MalformedSchematicError if KiCad's own parser rejects the working
    copy — the real file is left untouched. `force` does NOT bypass this:
    there is never a reason to overwrite a project with a file KiCad can't
    open.
    """
    s = get(schematic_path)
    if s is None:
        raise ValueError(f"no stage to commit for {schematic_path}")
    if s.is_stale() and not force:
        raise StaleStageError(
            f"real file modified after stage: {s.schematic_path} — pass force=True to overwrite"
        )

    # Vet the working copy with KiCad's own parser before it goes live: a
    # malformed file must never replace the user's project. kicad-cli is the
    # only check that reliably agrees with what KiCad will open — a hand-
    # rolled S-expr scan does not (a corrupted string can still balance by
    # naive rules yet be rejected by KiCad). Function-local import keeps the
    # staging<->kicad_cli dependency one-directional.
    from pinflow_api import kicad_cli

    reason = kicad_cli.validate_parseable(s.working_copy)
    if reason is not None:
        raise MalformedSchematicError(
            f"staged schematic for {s.schematic_path} is malformed "
            f"({reason}) — real file left untouched"
        )

    real = s.schematic_path
    tmp_dest = real.with_suffix(real.suffix + ".pinflow-tmp")
    tmp_dest.write_text(s.working_copy, encoding="utf-8")
    os.replace(tmp_dest, real)

    discard(real)
    return CommitResult(file_written=True)


def diff(schematic_path: Path) -> Optional[str]:
    """`git diff --no-index real staged` as text. None if no stage, "" if equal."""
    s = get(schematic_path)
    if s is None:
        return None
    try:
        r = subprocess.run(
            ["git", "diff", "--no-index", "--", str(s.schematic_path), str(s.temp_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # `git diff --no-index` exits 1 when files differ — that's not an error.
        return r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

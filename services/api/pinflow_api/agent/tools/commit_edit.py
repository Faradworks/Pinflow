"""Tool: commit_edit.

Writes the staged working copy to the real .kicad_sch on disk. Should be
called only after the user accepts the staged change via the UI (the system
prompt enforces an `ask_user` confirmation step before this).
"""

from __future__ import annotations

from pinflow_api import staging

SCHEMA = {
    "name": "commit_edit",
    "description": (
        "Commit the staged schematic to disk (replaces the real .kicad_sch). "
        "This is a plain file write — it does not touch git. Only call after "
        "the user has accepted the staged change via the UI."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def run(state, **_) -> dict:
    if state.active_sch_path is None:
        return {
            "status": "no_active_schematic",
            "hint": "Call read_active_schematic before commit_edit.",
        }

    if staging.get(state.active_sch_path) is None:
        return {"status": "no_stage", "hint": "Nothing staged to commit."}

    try:
        result = staging.commit(state.active_sch_path, force=False)
    except staging.StaleStageError as e:
        return {
            "status": "stale",
            "error": str(e),
            "hint": (
                "User saved in KiCad after the stage was created. "
                "Call discard_edit, then re-read the schematic and propose the edit again."
            ),
        }
    except staging.MalformedSchematicError as e:
        return {
            "status": "malformed",
            "error": str(e),
            "hint": (
                "The staged schematic is not a well-formed .kicad_sch — the "
                "commit was refused and the user's real file is untouched. "
                "This is a Pinflow bug in whatever produced the stage, not "
                "something the user can fix. Do NOT retry commit_edit. Tell "
                "the user the edit could not be committed safely and call "
                "discard_edit."
            ),
        }

    state.stage_stale = False
    return {
        "status": "ok",
        "file_written": result.file_written,
    }

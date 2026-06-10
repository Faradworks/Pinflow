"""Per-conversation attachment storage.

Files uploaded via `POST /agent/attachments` land in a tempdir keyed by
conversation_id. Refs are also stashed on the `ConversationState` so the
agent loop can mention them in the user message and tools can resolve
attachment_id → bytes.

MVP — disk + in-memory state both die on server restart, by design.
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class AttachmentRef:
    attachment_id: str
    filename: str
    mime: str
    size: int
    path: Path  # absolute path on disk
    parsed_mpn: Optional[str] = None  # set after parse_datasheet succeeds


_ROOT: Optional[Path] = None


def _root() -> Path:
    """Lazily-created tempdir root for all conversation attachments."""
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="pinflow_attach_"))
    return _ROOT


def conversation_dir(conversation_id: str) -> Path:
    d = _root() / conversation_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_attachment_id() -> str:
    return "att_" + uuid.uuid4().hex[:10]


def save(
    conversation_id: str,
    filename: str,
    mime: str,
    data: bytes,
) -> AttachmentRef:
    """Persist `data` to the conversation's tempdir and return its ref."""
    att_id = _new_attachment_id()
    # Keep the original filename for human readability; prefix the att_id so
    # listing the dir is sortable and there are no collisions.
    safe_name = filename.replace("/", "_").replace("\\", "_") or "upload"
    path = conversation_dir(conversation_id) / f"{att_id}__{safe_name}"
    path.write_bytes(data)
    return AttachmentRef(
        attachment_id=att_id,
        filename=filename,
        mime=mime,
        size=len(data),
        path=path,
    )

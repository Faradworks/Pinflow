"""POST /agent/chat — SSE stream of agent events.

Body contract: `{user_text: str, conversation_id?: str, attachment_ids?: [str]}`
for /chat, `{conversation_id: str, answer: str, attachment_ids?: [str]}` for
/chat/resume.

Attachments are uploaded first via `POST /agent/attachments` (multipart) which
returns ids the client then includes in chat/resume.

Each frame: `event: <kind>\\ndata: <json>\\n\\n`. Kinds are
`meta | ai | thinking | tool | action | system | suspended | done`.
The first `meta` event carries the (possibly server-generated)
conversation_id so the client can stash it for resume.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pinflow_api import llm
from pinflow_api.agent import attachments as attach_mod
from pinflow_api.agent import loop as agent_loop
from pinflow_api.agent import state as st
from pinflow_api.agent.events import to_sse

router = APIRouter(prefix="/agent")

# Per-stream JSONL traces of the SSE agent loop go here. Same dir the
# scripts/trace_chat.py CLI uses, so jq/grep tooling works on both. Each
# stream gets its own file so concurrent conversations don't interleave.
_TRACE_DIR = Path(__file__).resolve().parent.parent.parent / "_traces"


def _open_trace(kind: str, conv_id: str):
    """Open a JSONL trace for a single stream. Returns (sink, fh) where
    sink is a no-arg-required callable threaded into the agent loop and fh
    is the file handle to close after the stream ends."""
    _TRACE_DIR.mkdir(exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _TRACE_DIR / f"sse_{ts}_{kind}_{conv_id[:10]}.jsonl"
    fh = path.open("w", encoding="utf-8")

    def sink(record: dict) -> None:
        fh.write(json.dumps(record, default=str) + "\n")
        fh.flush()

    return sink, fh


class ChatBody(BaseModel):
    user_text: str
    conversation_id: Optional[str] = None
    attachment_ids: Optional[list[str]] = None


class ResumeBody(BaseModel):
    conversation_id: str
    answer: str
    attachment_ids: Optional[list[str]] = None


def _new_conversation_id() -> str:
    return "c_" + uuid.uuid4().hex[:12]


@router.post("/chat")
def chat(body: ChatBody, request: Request):
    conv_id = body.conversation_id or _new_conversation_id()
    sink, fh = _open_trace("chat", conv_id)
    llm_config = llm.config_from_headers(request.headers)

    def gen():
        try:
            for event in agent_loop.run_chat(
                conv_id,
                body.user_text,
                attachment_ids=body.attachment_ids or [],
                llm_config=llm_config,
                trace=sink,
            ):
                yield to_sse(event)
        finally:
            fh.close()

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/chat/resume")
def chat_resume(body: ResumeBody, request: Request):
    sink, fh = _open_trace("resume", body.conversation_id)
    llm_config = llm.config_from_headers(request.headers)

    def gen():
        try:
            for event in agent_loop.run_resume(
                body.conversation_id,
                body.answer,
                attachment_ids=body.attachment_ids or [],
                llm_config=llm_config,
                trace=sink,
            ):
                yield to_sse(event)
        finally:
            fh.close()

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/attachments")
async def upload_attachments(
    conversation_id: Optional[str] = Form(default=None),
    files: list[UploadFile] = File(...),
):
    """Upload one or more files. Returns the conversation_id (server-issued if
    none was supplied) plus a ref per file. Caller passes the returned
    `attachment_ids` in the next chat / resume body.
    """
    conv_id = conversation_id or _new_conversation_id()
    state = st.get_or_create(conv_id)

    refs = []
    for f in files:
        data = await f.read()
        ref = attach_mod.save(
            conversation_id=conv_id,
            filename=f.filename or "upload",
            mime=f.content_type or "application/octet-stream",
            data=data,
        )
        state.attachments[ref.attachment_id] = ref
        refs.append(
            {
                "attachment_id": ref.attachment_id,
                "filename": ref.filename,
                "mime": ref.mime,
                "size": ref.size,
            }
        )

    return {"conversation_id": conv_id, "attachments": refs}

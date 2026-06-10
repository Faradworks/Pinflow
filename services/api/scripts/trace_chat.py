"""Debug pipeline: run the chat agent loop on a prompt and show EVERYTHING.

Drives `pinflow_api.agent.loop` in-process (same pattern as
`scripts/smoke_variant_flow.py`) with a full-fidelity trace sink installed.
For the given prompt it renders, turn by turn, with nothing truncated away:

  - the exact system prompt + always-on context block sent to Anthropic
  - each LLM response: stop_reason, token usage, text, tool_use + full input
  - each tool call's complete input and the tool's complete return value
  - suspend/resume (`ask_user`) and the final stop reason

Console output is a readable transcript (long blobs soft-capped so the
terminal stays usable); a complete, uncapped JSONL trace is written to
`services/api/_traces/trace_<ts>.jsonl` for diffing runs or `jq`.

Usage:

    cd services/api
    .venv/bin/python scripts/trace_chat.py "add a TPS62840 buck, 5V to 3V3"
    .venv/bin/python scripts/trace_chat.py "parse this" --attach tests/fixtures/tps62843_datasheet.pdf
    .venv/bin/python scripts/trace_chat.py "add a buck" --answers "Confirm,Confirm"
    .venv/bin/python scripts/trace_chat.py "edit R1" --sch /path/to/board.kicad_sch

`ask_user` suspensions are answered from `--answers` (comma-separated, in
order) if supplied, otherwise interactively on stdin. Requires
services/api/.env with ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Optional

from pinflow_api.agent import loop as agent_loop
from pinflow_api.agent import state as st
from pinflow_api.agent.attachments import AttachmentRef

_API_ROOT = Path(__file__).resolve().parent.parent
_TRACE_DIR = _API_ROOT / "_traces"
_CONSOLE_CAP = 6000  # per-blob soft cap for the terminal; JSONL is never capped


# ── ANSI ────────────────────────────────────────────────────────────────────
class _C:
    enabled = sys.stdout.isatty()

    @classmethod
    def w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls.enabled else s


def _dim(s: str) -> str:
    return _C.w("2", s)


def _bold(s: str) -> str:
    return _C.w("1", s)


def _cyan(s: str) -> str:
    return _C.w("36", s)


def _yellow(s: str) -> str:
    return _C.w("33", s)


def _green(s: str) -> str:
    return _C.w("32", s)


def _red(s: str) -> str:
    return _C.w("31", s)


def _mag(s: str) -> str:
    return _C.w("35", s)


def _rule(label: str, color=_cyan) -> None:
    bar = "─" * max(4, 72 - len(label) - 2)
    print(f"\n{color('── ' + label + ' ')}{color(bar)}")


def _blob(obj: Any) -> str:
    """Pretty-print an arbitrary value, soft-capped for the console."""
    s = obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str)
    if len(s) > _CONSOLE_CAP:
        s = (
            s[:_CONSOLE_CAP]
            + _dim(
                f"\n… [{len(s) - _CONSOLE_CAP} more chars truncated for console; "
                "full payload in the JSONL trace]"
            )
        )
    return s


class _AbortStep(BaseException):
    """Raised from the step prompt to unwind the loop generator cleanly.

    BaseException (not Exception) on purpose: the loop wraps tool dispatch
    and the Anthropic call in `except Exception`, so a plain Exception here
    would be swallowed into a tool error instead of aborting the run.
    """


# Records the loop fires at natural decision boundaries. Pausing here in
# --step mode halts the loop *in place* (the sink runs synchronously inside
# the generator, in this thread), so you inspect what just happened — or
# what's about to run — before letting the next turn/tool proceed.
_STEP_AT = {
    "anthropic_request": "next LLM turn is about to be sent",
    "tool_call": "tool is about to run",
    "tool_result": "tool returned",
}


# ── trace sink ──────────────────────────────────────────────────────────────
class Tracer:
    """Renders trace records to the console and appends them to a JSONL file."""

    def __init__(self, jsonl_path: Path, *, step: bool = False) -> None:
        self._fh = jsonl_path.open("w", encoding="utf-8")
        self.jsonl_path = jsonl_path
        self.step = step and sys.stdin.isatty()

    def close(self) -> None:
        self._fh.close()

    def __call__(self, rec: dict) -> None:
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        getattr(self, f"_r_{rec['t']}", self._r_default)(rec)
        if self.step and rec["t"] in _STEP_AT:
            self._pause(rec["t"])

    def _pause(self, kind: str) -> None:
        prompt = (
            _bold(f"\n[step] {_STEP_AT[kind]} — ")
            + _dim("Enter=proceed · q=abort · c=run to end: ")
        )
        try:
            choice = input(prompt).strip().lower()
        except EOFError:
            print()
            raise _AbortStep
        if choice == "q":
            raise _AbortStep
        if choice == "c":
            self.step = False

    # one renderer per record type ------------------------------------------
    def _r_default(self, r: dict) -> None:
        _rule(r["t"], _dim)
        print(_blob({k: v for k, v in r.items() if k != "t"}))

    def _r_user_message(self, r: dict) -> None:
        _rule("USER MESSAGE", _green)
        print(_blob(r["text"]))

    def _r_resume(self, r: dict) -> None:
        _rule("RESUME (answer to ask_user)", _green)
        print(f"answer: {_bold(r['answer'])}  {_dim('→ ' + r['tool_use_id'])}")

    def _r_anthropic_request(self, r: dict) -> None:
        _rule(f"TURN {r['turn']} · ANTHROPIC REQUEST", _cyan)
        print(_dim(f"model={r['model']}  tools={len(r['tools'])}  "
                   f"messages={len(r['messages'])}"))
        print(_bold("\n[system prompt]"))
        print(_blob(r["system"]))
        print(_bold("\n[context block]"))
        print(_blob(r["context_block"]))
        # Full message history lives in the JSONL; show only the latest
        # message here so each turn's new input is visible without noise.
        if r["messages"]:
            print(_bold("\n[latest message in history]"))
            print(_blob(r["messages"][-1]))

    def _r_anthropic_response(self, r: dict) -> None:
        _rule(f"TURN {r['turn']} · ANTHROPIC RESPONSE", _cyan)
        u = r.get("usage") or {}
        print(_dim(f"stop_reason={r['stop_reason']}  "
                   f"in={u.get('input_tokens')} out={u.get('output_tokens')} tokens"))
        for b in r["content"]:
            if b.get("type") == "text":
                print(_bold("\n[text]"))
                print(_blob(b["text"]))
            elif b.get("type") == "tool_use":
                print(_mag(f"\n[tool_use] {b.get('name')}"))
                print(_blob(b.get("input")))
            else:
                print(_dim(f"\n[{b.get('type')}]"))

    def _r_tool_call(self, r: dict) -> None:
        _rule(f"TURN {r['turn']} · TOOL CALL  {r['tool']}", _yellow)
        print(_dim(f"tool_use_id={r['tool_use_id']}"))
        print(_bold("[input]"))
        print(_blob(r["input"]))

    def _r_tool_result(self, r: dict) -> None:
        result = r["result"]
        status = result.get("status") if isinstance(result, dict) else None
        color = _green if status in ("ok", "profile_ready") else (
            _red if status == "error" else _yellow
        )
        _rule(f"TURN {r['turn']} · TOOL RESULT  {r['tool']}  [{status}]", color)
        print(_blob(result))

    def _r_suspended(self, r: dict) -> None:
        _rule(f"TURN {r['turn']} · SUSPENDED (ask_user)", _mag)
        print(_bold(r["question"]))
        for i, opt in enumerate(r["options"], 1):
            print(f"  {i}. {opt}")
        if r.get("allow_freeform"):
            print(_dim("  (freeform answer allowed)"))

    def _r_stop(self, r: dict) -> None:
        _rule(f"TURN {r['turn']} · STOP — {r['reason']}", _cyan)

    def _r_error(self, r: dict) -> None:
        _rule(f"TURN {r['turn']} · ERROR @ {r['where']}", _red)
        print(_red(r["error"]))


# ── driver ──────────────────────────────────────────────────────────────────
_att_seq = 0


def _register_attachments(state: st.ConversationState, paths: list[str]) -> list[str]:
    global _att_seq
    ids: list[str] = []
    for p in paths:
        fp = Path(p).expanduser().resolve()
        if not fp.is_file():
            print(_red(f"attachment not found: {fp}"))
            sys.exit(2)
        aid = f"att_trace_{_att_seq}"
        _att_seq += 1
        mime = "application/pdf" if fp.suffix.lower() == ".pdf" else "application/octet-stream"
        state.attachments[aid] = AttachmentRef(
            attachment_id=aid,
            filename=fp.name,
            mime=mime,
            size=fp.stat().st_size,
            path=fp,
        )
        ids.append(aid)
        print(_dim(f"registered attachment {aid} ← {fp}"))
    return ids


def _drain(gen, tracer: Tracer) -> None:
    """Consume an event generator; the Tracer already rendered the detail."""
    for ev in gen:
        if ev.get("kind") == "system":
            print(_red(f"\n[system] {ev.get('text')}"))


def _next_answer(answers: list[str], options: list[str]) -> str:
    if answers:
        a = answers.pop(0)
        print(_green(f"\n→ auto-answer: {a}"))
        return a
    if options:
        for i, opt in enumerate(options, 1):
            print(f"  {i}. {opt}")
    raw = input(_bold("\nyour answer (number or text): ")).strip()
    if raw.isdigit() and options and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return raw


def _converse(
    conv_id: str,
    state: st.ConversationState,
    prompt: str,
    att_ids: list[str],
    answers: list[str],
    tracer: Tracer,
) -> None:
    """Send one user turn, then ride out any ask_user suspensions."""
    _drain(
        agent_loop.run_chat(conv_id, prompt, attachment_ids=att_ids, trace=tracer),
        tracer,
    )
    while state.pending_question is not None:
        pq = state.pending_question
        answer = _next_answer(answers, pq.options)
        _drain(agent_loop.run_resume(conv_id, answer, trace=tracer), tracer)


def _continuation(state: st.ConversationState) -> Optional[tuple[str, list[str]]]:
    """Interactively ask for a follow-up turn in the SAME conversation.

    Returns (message, attachment_ids) or None to end. Files are attached by
    typing one or more paths after a `+` token.
    """
    if not sys.stdin.isatty():
        return None
    try:
        raw = input(
            _bold("\n[conversation idle] next message ")
            + _dim("(blank to quit; attach files with +<path> tokens): ")
        ).strip()
    except EOFError:
        print()
        return None
    if not raw:
        return None
    words, paths = [], []
    for tok in raw.split():
        (paths if tok.startswith("+") else words).append(tok)
    paths = [p[1:] for p in paths]
    att_ids = _register_attachments(state, paths) if paths else []
    return " ".join(words), att_ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Trace the chat agent loop end to end.")
    ap.add_argument("prompt", help="the user prompt to send")
    ap.add_argument("--attach", action="append", default=[],
                    help="file to attach to the FIRST turn (repeatable)")
    ap.add_argument("--answers", default="",
                    help="comma-separated answers for ask_user, in order")
    ap.add_argument("--sch", default=None,
                    help="set active_sch_path on the conversation state")
    ap.add_argument("--conversation-id", default="trace")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--once", action="store_true",
                    help="don't prompt for follow-up turns; exit after the first")
    ap.add_argument("--step", action="store_true",
                    help="pause before each LLM turn and around every tool "
                         "call so you can inspect and choose to proceed")
    args = ap.parse_args()

    if args.no_color:
        _C.enabled = False

    _TRACE_DIR.mkdir(exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    tracer = Tracer(_TRACE_DIR / f"trace_{ts}.jsonl", step=args.step)
    print(_dim(f"JSONL trace → {tracer.jsonl_path}"))
    if args.step and not tracer.step:
        print(_dim("(--step ignored — stdin is not a TTY)"))

    state = st.get_or_create(args.conversation_id)
    if args.sch:
        # Pin the schematic so read_active_schematic reads/writes ONLY this file
        # and skips live KiCad detection. Without the pin, detect() resolves to
        # whatever project KiCad has open and the commit path mutates the user's
        # REAL .kicad_sch — exactly the footgun that bit us. active_sch_path is
        # also set for tools that read it before the first read_active_schematic.
        forced = Path(args.sch).expanduser().resolve()
        state.forced_sch_path = forced
        state.active_sch_path = forced
        print(_dim(f"pinned schematic (sandboxed) = {forced}"))
    att_ids = _register_attachments(state, args.attach)
    answers = [a for a in (args.answers.split(",") if args.answers else []) if a != ""]

    try:
        _converse(args.conversation_id, state, args.prompt, att_ids, answers, tracer)
        # The datasheet flow is multi-turn (model asks for the PDF, ends the
        # turn). Keep the SAME conversation alive so the user can reply and
        # attach the datasheet, exactly like the desktop app's chat.
        while not args.once:
            cont = _continuation(state)
            if cont is None:
                break
            msg, more_atts = cont
            _converse(args.conversation_id, state, msg, more_atts, answers, tracer)
    except _AbortStep:
        print(_red("\n[step] aborted by user"))
    finally:
        tracer.close()

    _rule("DONE", _cyan)
    print(_dim(f"full trace: {tracer.jsonl_path}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

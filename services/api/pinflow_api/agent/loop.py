"""Anthropic tool-use loop for the chat agent.

For each user message, this runs the LLM, dispatches tools, and yields
events that map 1:1 to the frontend `Message` discriminated union in
`apps/desktop/src/components/chat/types.ts`. Suspends when the model
calls `ask_user` — caller resumes via `run_resume()` with the user's
answer.

Sync generator on purpose. FastAPI's StreamingResponse will iterate it
inside its threadpool, which is fine for the blocking Anthropic SDK
calls we make here.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any, Callable, Iterator, Optional

from pinflow_api import cost
from pinflow_api import llm
from pinflow_api import staging
from pinflow_api.settings import settings

from . import events as ev
from . import state as st
from .context import build_context_block
from .tools import DISPATCH, TOOL_SCHEMAS


_SYSTEM = textwrap.dedent(
    """\
    You are Pinflow, a KiCad EDA agent. You help the user build and edit
    KiCad schematics through chat, calling tools for every action.

    Operating rules:
    - You have a toolbelt; use it. Don't describe what you would do — call
      the tool. Don't emit netlists, pin maps, or schematic source as prose
      — those are the tool's job. For clarifying questions, call `ask_user`.
    - For multi-IC requests ("USB power converter", "sensor frontend"),
      call `plan_block_diagram` FIRST, then `ask_user` to confirm the
      plan, then realize each block.
    - Every schematic edit lands in a staging area; nothing touches disk
      until the user confirms via the UI. Don't worry about overwriting
      their work.

    Which parts need parse_datasheet (don't waste turns):
    - parse_datasheet + design_spec are ONLY for the active IC/regulator
      (buck/boost/ldo controller etc.) — they exist to get its pinout and
      size its support passives. Connectors (USB-C, headers, jacks),
      discrete resistors/capacitors/inductors, LEDs, crystals, and other
      generic parts do NOT have a datasheet profile and must NOT go through
      parse_datasheet — it will just return needs_datasheet and burn a turn.
      Place those directly: `search_symbols` to find the lib_id, then build
      a netlist (every net needs `endpoints: [{ref, pin}]` — naming a net
      without endpoints wires nothing; see the tool's schema for the exact
      shape + a worked example) and call `add_subcircuit_from_netlist`.
      resolve_parts assigns their real LCSC part afterward.

    Generating subcircuits — the wired path (THREE steps, in order):
    - To add a subcircuit for any specific IC:
        1. `parse_datasheet(mpn=…, variant_hint?=…, attachment_id?=…)` —
           cold (PDF) or warm (cached) start. Resolves the IC variant +
           a matching KiCad symbol. On success returns
           `status:"profile_ready"` (it no longer returns a netlist).
        2. `design_spec(mpn=…, topology=<buck|boost|buck_boost|ldo>,
           vin=…, vout=…, vref?=…, fsw_hz?=…, iout_a?=…, role?=…)` —
           runs deterministic equations (feedback divider, inductor,
           in/out caps), shows the user a reviewable design spec, and
           returns a synthesized `netlist`. Supply `vref` (datasheet
           feedback reference) for any adjustable-Vout regulator.
        3. After `design_spec` returns `status:"ok"`, run the FIRST
           confirm gate: `ask_user(question="Apply this design spec?",
           options=["Confirm","Discard"])`. On "Confirm", call
           `add_subcircuit_from_netlist(netlist=<the netlist from the
           design_spec result>, port_bindings?=…, label=…)` to place +
           stage. On "Discard", end the turn without staging.
        4. After `add_subcircuit_from_netlist` stages the block, call
           `resolve_parts()` so every placed component gets a real LCSC
           part (MPN/LCSC/Manufacturer/Description filled). It auto-picks
           the best candidate per part and stages the result.
      The user's schematic viewer shows the staged result after step 3's
      add_subcircuit_from_netlist (and again after step 4's resolve_parts);
      the staging discipline below then adds the SECOND confirm gate
      (commit/discard the staged schematic — run it ONCE after step 4, so
      the user confirms placement + resolved parts together).
    - Do NOT say "the placer isn't available" or "you'll need to place
      manually". Those statements are wrong. If a tool fails, surface the
      actual error from the tool result — never invent a "feature not
      ready" explanation.

    Replicating / duplicating an existing block ("add another LDO",
    "duplicate the buck", "add a second regulator like U1") — don't waste turns:
    - The `read_active_schematic` digest ALREADY enumerates the source block:
      its IC (the exact installed `lib_id`, package, every pin → net) and its
      local support parts. Build the new netlist DIRECTLY from that digest and
      call `add_subcircuit_from_netlist`. REUSE the source IC's `lib_id` from
      the digest verbatim — don't spend a turn on `search_symbols` to
      rediscover a symbol the digest already names. And do NOT call
      `extract_subgraph` just to "see" a block the digest already describes —
      `extract_subgraph` is for pulling a fragment you intend to transplant or
      modify, not for re-reading the schematic, and it returns a wasted turn
      here.
    - `extract_subgraph` also CANNOT recover a fixed-function block's bypass
      caps when the block sits on shared global rails (+5V / +3V3 / GND): on
      shared rails those caps are indistinguishable from the rest of the
      board's rail decoupling, so neither the digest nor an extraction
      attributes them to one IC. Just give the duplicate its OWN standard
      decoupling inline (e.g. an LDO: a 10µF input cap and a 22µF output cap).
    - A duplicated part shares the original's MPN, so it shares its LCSC part
      too. Set the same Value/MPN on the clone and SKIP `resolve_parts` — the
      catalogue search is redundant (and fails outright when the parts
      catalogue is offline) when an identical sibling already exists in the design; its
      orderable part can be copied from that sibling. Reserve `resolve_parts`
      for genuinely new parts that have no sibling to copy from.

    Finding symbols (avoid the no_symbol thrash):
    - Before putting a `lib_id` you're not certain of into a netlist, call
      `search_symbols(query=…)` to get the EXACT lib_id installed on this
      machine. KiCad symbol names differ across versions/installs, so
      guessing from memory (e.g. `Connector_USB:USB_C_Receptacle_USB2.0`)
      often fails with `no_symbol`. One search beats several failed guesses.
    - If `add_subcircuit_from_netlist` returns `no_symbol`, do NOT retry with
      another guessed lib_id — call `search_symbols` for that part first,
      then retry with a returned lib_id. `search_symbols` is for KiCad
      symbols; `search_parts` is the separate LCSC catalogue.

    Datasheet ingest — the wired path:
    - If `parse_datasheet` (or `get_component_profile`) returns
      `status:"needs_datasheet"`, request the datasheet PDF in a plain
      text reply and END THE TURN. Do NOT call `ask_user` for this —
      attaching a file isn't a multiple-choice question. Say something
      like: "I don't have a cached profile for <MPN>. Please attach its
      datasheet PDF (paperclip in the chat box, or drag it in) and send."
      Then stop — wait for the user's next message.
    - When a subsequent user message contains an `[Attachments from user]`
      marker listing one or more attachment_ids, call
      `parse_datasheet(attachment_id=…, mpn=…)`. The MPN must be the
      canonical form (e.g. "TPS62840", not "TPS62840DLCR" — use
      `variant_hint` for variant selection).
    - Within a conversation, attachment_ids stay alive — if the user has
      already attached the datasheet earlier and you want to re-parse
      with a different `variant_hint`, reuse the SAME `attachment_id`
      from the earlier `[Attachments from user]` marker.
    - Never invent attachment_ids — only use ones that appear verbatim in
      an `[Attachments from user]` marker in the conversation.
    - If `parse_datasheet` returns `status:"needs_lcsc_choice"`, the
      bundled KiCad libs don't carry a matching symbol but the parts
      catalogue found multiple LCSC candidates with the same MPN.
      Call `ask_user(question="Which LCSC part should I use?",
      options=[<one entry per candidate>])` — for each candidate, build
      an option label like `"<lcsc_code> — <mpn> (<manufacturer>,
      <package>, stock=<stock>)"` so the user can disambiguate. Then
      re-call `parse_datasheet` with `lcsc_code=<the code from the chosen
      option>` plus the same `mpn` and other args. NEVER invent an
      `lcsc_code` — only use codes from the candidates list.

    Staging discipline (enforced by you, the agent):
    - This is the SECOND confirm gate (the first is the design-spec review
      above). It is separate and always required.
    - After any tool that stages an edit (e.g. `add_subcircuit_from_netlist`,
      `resolve_mpn` writeback, or any future *_edit tool), ALWAYS follow up
      with `ask_user(question="Apply these changes?", options=["Confirm","Discard"])`.
      Use exactly those two option labels.
    - When the answer comes back, the system AUTOMATICALLY runs `commit_edit`
      (on "Confirm") or `discard_edit` (on "Discard") and hands you the outcome
      in the tool result. You normally do NOT call them yourself — just read
      that outcome and give the user a short confirmation (or, if it reports a
      non-ok status, follow its hint). Calling `commit_edit` again only wastes a
      turn; the stage is already gone. (`commit_edit` / `discard_edit` stay
      callable for the rare case you must commit/discard outside this gate.)
    - Never assume the user accepted. Never `commit_edit` without an explicit
      Confirm answer.

    Missing symbols (no bundled match):
    - If `search_symbols` finds nothing usable for a part, that part has no
      bundled KiCad symbol. If you have an LCSC code for it (from
      `search_parts`/`resolve_parts`), call
      `install_symbol_to_project(lcsc_code=…)` — it fetches the symbol, adds
      it to the project, and returns a `pinflow:<symbol>` lib_id plus the
      pins. Use that lib_id (and those pin numbers) in your netlist. Only do
      this for genuinely-absent symbols; bundled symbols need no install.

    Tool inventory — wired vs stubbed:
    - WIRED (real implementations, use freely):
      `read_active_schematic`, `get_component_profile`, `plan_block_diagram`,
      `ask_user`, `parse_datasheet`, `design_spec`,
      `add_subcircuit_from_netlist`, `commit_edit`, `discard_edit`,
      `resolve_mpn`, `resolve_parts`, `run_erc`, `search_parts`,
      `search_symbols`, `install_symbol_to_project`, `extract_subgraph`,
      `edit_property`.
    - STUBBED (return `{"status":"not_implemented"}`):
      `remove_components`, `select_part`, `read_datasheet_section`.
    - When a stubbed tool would be the right call, briefly tell the user
      the specific feature isn't wired yet — do NOT generalize to "the
      placer/agent isn't available". Offer the closest wired alternative.
    """
).strip()


_MAX_TURNS = 12  # safety bound — generous; the breaker below stops true stalls

# Circuit breaker for tool-failure spirals. A tool returning the SAME status
# this many times within one _drive — counted CUMULATIVELY (the model
# interleaves search_symbols/install retries between failed attempts, so a
# strictly-consecutive counter never trips) — stops the loop and surfaces the
# tool's own hint, instead of grinding to _MAX_TURNS with a useless "max turns
# reached". We count any status that ISN'T progress: a denylist of *failures*
# is fragile (add_subcircuit_from_netlist alone returns bad_netlist /
# validation_failed / placer_failed / no_symbol / …, and an earlier allowlist
# silently missed the last two), so instead everything is a potential stall
# EXCEPT the known progress / need-user-input statuses below. This pairs with
# temperature 0: identical input yields identical output, so a repeated
# identical (tool, status) is a genuine deterministic stall — not exploration —
# which makes the breaker precise. A tool's tally clears when it next succeeds.
_REPEAT_FAIL_LIMIT = 3
_BREAKER_SUCCESS_STATUSES = frozenset({"ok", "profile_ready"})  # clears the tally
_BREAKER_NEEDS_INPUT_STATUSES = frozenset({"needs_datasheet", "needs_lcsc_choice", "signin_required"})  # ignored


def _superseded_result(tool_use_id: str) -> dict:
    """Synthetic tool_result for a duplicate ask_user in one response — only
    one question can suspend, but every tool_use MUST get a tool_result or
    the dangling id 400s every later request and bricks the conversation."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(
            {
                "status": "superseded",
                "hint": (
                    "Only one ask_user per turn is supported; this question "
                    "was NOT shown to the user. Re-ask it after the first "
                    "answer arrives if still relevant."
                ),
            }
        ),
    }


def _breaker_message(tool: str, status: str, result: Optional[dict]) -> str:
    """Honest, actionable dead-end message when the breaker trips — surfaces the
    tool's real error + hint (never a fabricated 'feature not ready')."""
    detail = ""
    if isinstance(result, dict):
        errs = result.get("errors") or result.get("error")
        if isinstance(errs, list):
            errs = "; ".join(str(e) for e in errs[:4])
        if errs:
            detail += f" Last error: {errs}."
        hint = result.get("hint")
        if hint:
            detail += f" {hint}"
    return (
        f"Stopped: `{tool}` returned {status} {_REPEAT_FAIL_LIMIT}× this turn "
        f"without making progress — not going to keep retrying.{detail}"
    )


# A trace sink is an optional, untruncated tap on the loop's internals for
# debugging. Production (the SSE route) passes nothing — zero overhead. The
# CLI driver `scripts/trace_chat.py` passes a sink that records every
# Anthropic request/response and every tool call/result at full fidelity.
TraceSink = Callable[[dict], None]


def _trace(sink: Optional[TraceSink], **record: Any) -> None:
    if sink is not None:
        sink(record)


def _serialize_response(response) -> dict:
    """Full-fidelity dump of an Anthropic message response for tracing."""
    usage = getattr(response, "usage", None)
    return {
        "stop_reason": response.stop_reason,
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cache_creation_input_tokens": getattr(
                usage, "cache_creation_input_tokens", None
            ),
            "cache_read_input_tokens": getattr(
                usage, "cache_read_input_tokens", None
            ),
        }
        if usage is not None
        else None,
        "content": [_serialize_block(b) for b in response.content],
    }


def run_chat(
    conversation_id: str,
    user_text: str,
    attachment_ids: list[str] | None = None,
    *,
    llm_config: Optional["llm.LLMConfig"] = None,
    trace: Optional[TraceSink] = None,
) -> Iterator[dict]:
    """Entry point for a new user message."""
    state = st.get_or_create(conversation_id)
    if llm_config is not None:
        state.llm = llm_config
    body = _user_text_with_attachments(state, user_text, attachment_ids)
    state.messages.append({"role": "user", "content": body})
    state.cost.reset_request()  # new user message → fresh per-request meter + cap
    _trace(trace, t="user_message", text=body)
    yield ev.ev_meta(conversation_id)
    yield from _drive(state, trace=trace)


def _is_commit_gate(state: st.ConversationState, pending: st.PendingQuestion) -> bool:
    """True when the pending question is the post-stage commit/discard gate.

    The design-spec gate and the commit gate share the same Confirm/Discard
    options, so options alone can't tell them apart. The disambiguator is
    stage presence: the commit gate is only ever asked AFTER a working copy
    is staged, whereas the design-spec gate is asked BEFORE anything is placed
    (add_subcircuit_from_netlist hasn't run yet). `staging.commit` drops the
    stage on success, so a prior committed block never leaves one behind to
    confuse this.
    """
    opts = {str(o).strip().lower() for o in (pending.options or [])}
    if opts != {"confirm", "discard"}:
        return False
    if state.active_sch_path is None:
        return False
    return staging.get(state.active_sch_path) is not None


def run_resume(
    conversation_id: str,
    answer: str,
    attachment_ids: list[str] | None = None,
    *,
    llm_config: Optional["llm.LLMConfig"] = None,
    trace: Optional[TraceSink] = None,
) -> Iterator[dict]:
    """Resume a suspended conversation with the user's answer to ask_user."""
    state = st.get(conversation_id)
    if state is None:
        yield ev.ev_system(f"unknown conversation: {conversation_id}")
        yield ev.ev_done()
        return
    if llm_config is not None:
        state.llm = llm_config
    pending = state.pending_question
    if pending is None:
        yield ev.ev_system("no pending question to answer in this conversation")
        yield ev.ev_done()
        return

    state.pending_question = None

    # Cost-cap gate: a Continue/Stop on the per-request spend gate has no backing
    # tool_use to answer — it just decides whether to keep driving. Handle it
    # before the tool_result path (which would 400 on the synthetic tool_use_id).
    if pending.kind == "cost_cap":
        yield ev.ev_meta(conversation_id)
        if answer.strip().lower() in ("stop", "cancel", "halt", "no"):
            _trace(trace, t="cost_cap_resume", answer=answer, decision="stop")
            yield ev.ev_system(
                "Stopped before spending more credits. Send a new message to continue."
            )
            yield ev.ev_done()
            return
        # Continue: approve the REST of this request — don't nag again on every
        # subsequent turn (one confirmation per request, not per cap-crossing).
        # reset_request() re-arms the gate on the next user message.
        state.cost.approved_ceiling = float("inf")
        _trace(trace, t="cost_cap_resume", answer=answer, decision="continue")
        yield from _drive(state, trace=trace)
        return

    # The user's answer goes back as the tool_result for the original ask_user.
    # When the model emitted other tool_use blocks alongside ask_user in the
    # same response, those were dispatched eagerly and stashed in
    # state.pending_tool_results — flush them in the SAME user message, since
    # Anthropic requires every tool_use in a response to be answered together.
    sibling_results = state.pending_tool_results
    state.pending_tool_results = []

    yield ev.ev_meta(conversation_id)

    # Commit-gate fast path: a Confirm/Discard on a staged edit deterministically
    # maps to commit_edit / discard_edit. Run it HERE instead of bouncing back
    # through a whole LLM turn whose entire output is `commit_edit({})` (the
    # answer fully determines the action — the model adds nothing to the
    # *decision*). We still re-enter the loop afterwards with the outcome folded
    # into this tool_result, so the model gets its one post-commit turn to
    # summarize for the user (where it DOES add value). Net: one fewer LLM turn
    # per staged edit. Non-Confirm/Discard answers fall through to the model.
    answer_payload: dict = {"answer": answer}
    answer_norm = answer.strip().lower()
    if _is_commit_gate(state, pending) and answer_norm in ("confirm", "discard"):
        gate_tool = "commit_edit" if answer_norm == "confirm" else "discard_edit"
        yield ev.ev_tool(gate_tool, title=gate_tool, meta=[])
        _trace(trace, t="tool_call", turn=0, tool=gate_tool,
               tool_use_id=pending.tool_use_id, input={})
        try:
            gate_result = DISPATCH[gate_tool](state)
        except Exception as e:  # never let a gate failure 500 the resume
            gate_result = {"status": "error", "error": str(e)}
        _trace(trace, t="tool_result", turn=0, tool=gate_tool,
               tool_use_id=pending.tool_use_id, result=gate_result)
        status = gate_result.get("status") if isinstance(gate_result, dict) else None
        if status == "ok":
            note = (
                f"{gate_tool} was already executed automatically on the user's "
                f"'{answer}'. Do NOT call {gate_tool} again — just confirm the "
                "outcome to the user in a short message and end the turn."
            )
        else:
            note = (
                f"An automatic {gate_tool} attempt returned status={status!r}. "
                "Follow the result's hint to resolve it, then tell the user."
            )
        answer_payload = {
            "answer": answer,
            "auto_dispatched": gate_tool,
            "result": gate_result,
            "note": note,
        }

    answer_result = {
        "type": "tool_result",
        "tool_use_id": pending.tool_use_id,
        "content": json.dumps(answer_payload),
    }
    # When the user also attached files, those need to appear in the
    # conversation too — append a follow-up user message after the tool_result
    # so the model sees them. (Anthropic requires tool_result first.)
    state.messages.append(
        {
            "role": "user",
            "content": sibling_results + [answer_result],
        }
    )
    _trace(trace, t="resume", answer=answer, tool_use_id=pending.tool_use_id)
    note = _attachments_note(state, attachment_ids)
    if note:
        state.messages.append({"role": "user", "content": note})
    yield from _drive(state, trace=trace)


def _user_text_with_attachments(
    state: st.ConversationState,
    user_text: str,
    attachment_ids: list[str] | None,
) -> str:
    """Build the user-message body, prepending an [Attachments: ...] marker
    when the user uploaded files alongside this turn."""
    note = _attachments_note(state, attachment_ids)
    if not note:
        return user_text
    return f"{note}\n\n{user_text}" if user_text.strip() else note


def _attachments_note(
    state: st.ConversationState,
    attachment_ids: list[str] | None,
) -> str:
    if not attachment_ids:
        return ""
    rows = []
    for aid in attachment_ids:
        ref = state.attachments.get(aid)
        if ref is None:
            rows.append(f"- {aid} (missing — upload failed?)")
            continue
        rows.append(f"- {ref.attachment_id} ({ref.filename}, {ref.mime}, {ref.size} bytes)")
    return "[Attachments from user]\n" + "\n".join(rows)


def _friendly_llm_error(e: Exception) -> str:
    """Map gateway/LLM errors to a user-facing message. The Pinflow Cloud gateway
    returns 402 (out of credits) and 401 (expired session) — surface those
    clearly instead of a raw 'anthropic error'."""
    status = getattr(e, "status_code", None)
    text = str(e)
    if status == 402 or "insufficient_credits" in text:
        return (
            "⚡ You're out of Pinflow credits. Open the Backend & AI provider "
            "settings (the sliders in the title bar) to top up, then try again."
        )
    if status == 401 and ("expired" in text.lower() or "pinflow" in text.lower()):
        return "Your Pinflow Cloud session expired — open settings and sign in again."
    return f"anthropic error: {e}"


def _drive(
    state: st.ConversationState,
    *,
    trace: Optional[TraceSink] = None,
) -> Iterator[dict]:
    """Run LLM turns until end_turn or ask_user suspension."""
    if not llm.available(state.llm):
        yield ev.ev_system(llm.NOT_CONFIGURED_MSG)
        yield ev.ev_done()
        return

    try:
        client = llm.make_client(state.llm)
    except Exception as e:
        yield ev.ev_system(f"failed to construct anthropic client: {e}")
        yield ev.ev_done()
        return

    # Tool-failure-spiral breaker state (see _REPEAT_FAIL_LIMIT). Spans turns
    # within this drive so interleaved retries still accumulate.
    fail_counts: dict[tuple[str, str], int] = {}
    tripped: Optional[tuple[str, str]] = None
    tripped_result: Optional[dict] = None

    # Hero path runs on the stronger agent model (Opus by default); extraction/
    # emit stay on settings.anthropic_model.
    model = settings.anthropic_agent_model or settings.anthropic_model
    provider = llm.provider_of(state.llm)  # labels the live cost meter

    for turn in range(1, _MAX_TURNS + 1):
        context_block = build_context_block(state)
        _trace(
            trace,
            t="anthropic_request",
            turn=turn,
            model=model,
            system=_SYSTEM,
            context_block=context_block,
            tools=[s["name"] for s in TOOL_SCHEMAS],
            messages=state.messages,
        )
        try:
            # with_raw_response (not .create) so we can read the gateway's
            # per-call credit headers off `raw.headers`; `.parse()` returns the
            # same Message .create() would have. Zero behavior change otherwise.
            raw = client.messages.with_raw_response.create(
                model=model,
                max_tokens=4096,
                # Two prompt-cache breakpoints. The first caches the stable
                # prefix (tools + _SYSTEM) — unchanged across every turn and
                # every conversation, so it survives within the 5-min ephemeral
                # TTL. The second extends the cached prefix through the context
                # block: that digest can be huge on a large schematic but is
                # stable across the up-to-_MAX_TURNS LLM round-trips of a
                # single user prompt, so we pay to embed it once per prompt and
                # read it from cache on every subsequent loop turn. If the
                # context ever changes mid-loop the longer prefix just misses
                # and falls back to the tools+_SYSTEM cache — no correctness
                # impact, since the real current block is always sent.
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": "Current context:\n" + context_block,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                tools=TOOL_SCHEMAS,
                messages=state.messages,
            )
            response = raw.parse()
        except Exception as e:
            _trace(trace, t="error", turn=turn, where="anthropic", error=str(e))
            yield ev.ev_system(_friendly_llm_error(e))
            yield ev.ev_done()
            return

        _trace(
            trace,
            t="anthropic_response",
            turn=turn,
            **_serialize_response(response),
        )

        # Cost meter: on the cloud path the gateway's per-call headers drive an
        # authoritative balance-delta (which also captures tool-internal calls);
        # off-gateway (self/BYOK) we show a local USD estimate. Surfaced live so
        # the UI shows running spend before the post-turn balance refresh. Never
        # let metering break a turn.
        cost_event = None
        try:
            charged, balance = cost.parse_gateway_credits(getattr(raw, "headers", None))
            usd = cost.call_cost_usd(model, getattr(response, "usage", None))
            state.cost.record(charged=charged, balance=balance, usd=usd)
            cost_event = ev.ev_cost(state.cost, model=model, provider=provider)
        except Exception as e:  # pragma: no cover — defensive
            _trace(trace, t="error", turn=turn, where="cost_meter", error=str(e))
        if cost_event is not None:
            _trace(trace, t="cost", turn=turn, **{k: v for k, v in cost_event.items() if k != "kind"})
            yield cost_event

        # Persist the assistant turn into state in SDK-shape.
        state.messages.append(
            {
                "role": "assistant",
                "content": [_serialize_block(b) for b in response.content],
            }
        )

        # Emit text first, then iterate tool_use blocks.
        text = "\n".join(
            b.text.strip()
            for b in response.content
            if getattr(b, "type", None) == "text" and b.text.strip()
        )
        if text:
            yield ev.ev_ai(text)

        if response.stop_reason != "tool_use":
            _trace(trace, t="stop", turn=turn, reason=response.stop_reason)
            yield ev.ev_done()
            return

        tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        tool_results: list[dict] = []
        ask_user_tu = None  # deferred; processed after sibling dispatches

        for tu in tool_uses:
            name = tu.name
            tool_input: dict[str, Any] = dict(tu.input or {})

            # ask_user suspends the loop. Defer it so any sibling tool_uses
            # in the same response still get dispatched + result-appended
            # this turn — Anthropic rejects the next request otherwise
            # ("tool_use ids were found without tool_result blocks").
            #
            # Only ONE ask_user can suspend: the resume answer reattaches to a
            # single tool_use_id. The model occasionally emits two ask_user
            # calls in one response — the extras must still get a tool_result
            # (synthetic, here) or the dangling id 400s every subsequent
            # request and permanently bricks the conversation.
            if name == "ask_user":
                if ask_user_tu is None:
                    ask_user_tu = tu
                else:
                    _trace(trace, t="tool_result", turn=turn, tool=name,
                           tool_use_id=tu.id, result={"status": "superseded"})
                    tool_results.append(_superseded_result(tu.id))
                continue

            # Normal tool dispatch.
            meta = [{"k": k, "v": _short(v)} for k, v in tool_input.items()][:6]
            yield ev.ev_tool(name, title=name, meta=meta)
            _trace(
                trace,
                t="tool_call",
                turn=turn,
                tool=name,
                tool_use_id=tu.id,
                input=tool_input,
            )

            fn = DISPATCH.get(name)
            if fn is None:
                result: dict = {"status": "error", "error": f"unknown tool: {name}"}
            else:
                try:
                    # Scope the conversation's LLM provider so a tool building
                    # its own Anthropic client (parse_datasheet, design_spec, …)
                    # routes/meters through the same provider as the turn.
                    with llm.llm_scope(state.llm):
                        result = fn(state, **tool_input)
                except Exception as e:
                    result = {"status": "error", "error": str(e)}

            _trace(
                trace,
                t="tool_result",
                turn=turn,
                tool=name,
                tool_use_id=tu.id,
                result=result,
            )

            # Update the failure-spiral breaker (see _REPEAT_FAIL_LIMIT). Count
            # ANY status that isn't progress or a wait-for-user — a denylist of
            # failure strings kept missing real ones (validation_failed /
            # placer_failed). A success clears that tool's tally.
            status = result.get("status") if isinstance(result, dict) else None
            if status in _BREAKER_SUCCESS_STATUSES:
                for k in [key for key in fail_counts if key[0] == name]:
                    del fail_counts[k]  # this tool made progress — forget its failures
            elif status is not None and status not in _BREAKER_NEEDS_INPUT_STATUSES:
                sig = (name, status)
                fail_counts[sig] = fail_counts.get(sig, 0) + 1
                if fail_counts[sig] >= _REPEAT_FAIL_LIMIT and tripped is None:
                    tripped, tripped_result = sig, result

            # Surface the planner's output as a BlockDiagramCard in chat.
            if (
                name == "plan_block_diagram"
                and isinstance(result, dict)
                and result.get("status") == "ok"
            ):
                yield ev.ev_block_diagram(
                    nodes=list(result.get("nodes", [])),
                    edges=list(result.get("edges", [])),
                )

            # Surface the design abstract as a DesignSpecCard in chat
            # (mirrors plan_block_diagram; the model then runs the
            # Confirm/Discard gate on it).
            if (
                name == "design_spec"
                and isinstance(result, dict)
                and result.get("status") == "ok"
            ):
                yield ev.ev_design_spec(spec=result.get("spec", {}))

            # Surface the part-resolution table as a ResolvePartsCard; the
            # model then runs the Confirm/Discard gate on it (mirrors
            # design_spec — this is the post-stage commit gate, not a new one).
            if (
                name == "resolve_parts"
                and isinstance(result, dict)
                and result.get("status") == "ok"
            ):
                yield ev.ev_resolve_parts(rows=list(result.get("resolved", [])))

            # Any parts tool reporting it needs a (free) Pinflow sign-in → a
            # SignInCard with one-click sign-in that keeps BYOK for the LLM.
            # search_parts uses status; parse_datasheet carries a signin_required
            # flag on its needs_datasheet result. The agent's text offers the
            # manual MPN/PDF fallback alongside — the card never blocks.
            if isinstance(result, dict) and (
                result.get("status") == "signin_required"
                or result.get("signin_required") is True
            ):
                yield ev.ev_signin_required(hint=str(result.get("hint", "")))

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result),
                }
            )

        # Suspend on ask_user AFTER siblings dispatched. Their tool_results
        # are stashed for run_resume to flush together with the user's answer.
        if ask_user_tu is not None:
            tu = ask_user_tu
            tool_input = dict(tu.input or {})
            question_text = str(tool_input.get("question", ""))
            options = list(tool_input.get("options", []) or [])
            allow_freeform = bool(tool_input.get("allow_freeform", False))
            _trace(
                trace,
                t="suspended",
                turn=turn,
                tool_use_id=tu.id,
                question=question_text,
                options=options,
                allow_freeform=allow_freeform,
                sibling_tool_results=len(tool_results),
            )

            yield ev.ev_tool(
                "ask_user",
                title="asking user",
                meta=[{"k": "question", "v": _short(question_text)}],
            )
            qid = "q_" + tu.id[:10]
            # On a Confirm/Discard gate, attach a fuzzy "cost to finish from here"
            # estimate (rendered on the ConfirmBar) — an honest pre-execution
            # number for the bounded remaining work, since the whole-conversation
            # cost is unknowable up front. Staging presence picks the gate type.
            gate_cost = None
            if {str(o).strip().lower() for o in options} == {"confirm", "discard"}:
                staged = (
                    state.active_sch_path is not None
                    and staging.get(state.active_sch_path) is not None
                )
                try:
                    gate_cost = cost.gate_estimate(
                        state.cost, model, state.messages,
                        staged=staged, provider=provider,
                    )
                except Exception:
                    gate_cost = None
            yield ev.ev_ai(
                question_text,
                questions=[{"id": qid, "q": question_text, "options": options}],
                locked=False,
                cost=gate_cost,
            )

            state.pending_question = st.PendingQuestion(
                tool_use_id=tu.id,
                question_id=qid,
                options=options,
                allow_freeform=allow_freeform,
            )
            state.pending_tool_results = tool_results
            yield ev.ev_suspended()
            return

        state.messages.append({"role": "user", "content": tool_results})

        if tripped is not None:
            tool_name, status = tripped
            _trace(
                trace,
                t="stop",
                turn=turn,
                reason="repeated_failure",
                tool=tool_name,
                status=status,
            )
            yield ev.ev_system(_breaker_message(tool_name, status, tripped_result))
            yield ev.ev_done()
            return

        # Per-request spend cap: if this user message's running cost crossed the
        # configured ceiling, pause and ask before driving another LLM turn. The
        # ceiling is the cap, bumped each time the user clicks Continue (see the
        # cost_cap branch in run_resume). Disabled when the cap is 0.
        cap = settings.pinflow_credit_cap_per_request
        if cap > 0:
            ceiling = state.cost.approved_ceiling
            if ceiling is None:
                ceiling = cap
            if state.cost.request_credits >= ceiling:
                _trace(
                    trace,
                    t="cost_cap",
                    turn=turn,
                    spent=state.cost.request_credits,
                    cap=cap,
                )
                yield from _suspend_cost_cap(state, turn)
                return
        # Loop back for next LLM turn.

    # Hit the safety bound.
    _trace(trace, t="stop", turn=_MAX_TURNS, reason="max_turns")
    yield ev.ev_system(f"max turns ({_MAX_TURNS}) reached — stopping")
    yield ev.ev_done()


def _suspend_cost_cap(state: st.ConversationState, turn: int) -> Iterator[dict]:
    """Suspend the loop at the per-request spend gate. Emits a Continue/Stop
    question (rendered as a normal QuestionsCard) and stashes a `cost_cap`
    PendingQuestion; `run_resume` decides whether to keep driving. Reuses the
    ask_user suspend/resume plumbing — no backing tool_use, so no tool_result."""
    spent = round(state.cost.request_credits, 2)
    cap = settings.pinflow_credit_cap_per_request
    msg = (
        f"This request has used about {spent} credits, hitting your {cap:g}-credit "
        f"per-request limit. Continue with the rest of this request?"
    )
    qid = f"q_cap_{turn}"
    yield ev.ev_tool("cost_cap", title="spend limit", meta=[{"k": "used", "v": str(spent)}])
    yield ev.ev_ai(
        msg,
        questions=[{"id": qid, "q": msg, "options": ["Continue", "Stop"]}],
        locked=False,
    )
    state.pending_question = st.PendingQuestion(
        tool_use_id="__cost_cap__",
        question_id=qid,
        options=["Continue", "Stop"],
        allow_freeform=False,
        kind="cost_cap",
    )
    state.pending_tool_results = []  # nothing pending; messages already valid
    yield ev.ev_suspended()


def _serialize_block(block) -> dict:
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.input or {}),
        }
    return {"type": btype or "unknown"}


def _short(v: Any, limit: int = 80) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"

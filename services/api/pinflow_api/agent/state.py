"""In-memory per-conversation state.

Dropped on server restart by design — MVP. Statelessness will bite later but
is fine for now.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from pinflow_api.cost import CostMeter

if TYPE_CHECKING:
    from pinflow_api.agent.attachments import AttachmentRef
    from pinflow_api.design_spec import DesignSpec
    from pinflow_api.graph.models import DesignGraph
    from pinflow_api.llm import LLMConfig
    from pinflow_api.profile import ComponentProfile


@dataclass
class PendingQuestion:
    """Stashed when the model calls ask_user; the loop suspends until resume.

    `kind` tells `run_resume` how to interpret the answer: "ask_user" (the
    default) reattaches it as a tool_result to `tool_use_id`; "cost_cap" is the
    per-request spend gate, which has no backing tool_use — a Continue/Stop just
    resumes or ends the drive (see agent/loop.py).
    """

    tool_use_id: str
    question_id: str
    options: list[str]
    allow_freeform: bool
    kind: str = "ask_user"


@dataclass
class ConversationState:
    conversation_id: str
    messages: list[dict] = field(default_factory=list)  # Anthropic SDK message format
    pending_question: Optional[PendingQuestion] = None
    # When the model emits ask_user alongside other tool_use blocks in the
    # same response, Anthropic requires ALL tool_use ids to be answered in
    # the next user message. We dispatch the non-ask_user ones immediately,
    # stash their tool_results here, and on resume flush them together with
    # the user's answer to ask_user. Cleared after each flush.
    pending_tool_results: list[dict] = field(default_factory=list)
    # Populated by read_active_schematic; consumed by digest in context.py
    # and by edit-side tools (commit/discard/add_subcircuit/…).
    active_sch_path: Optional[Path] = None
    # Debug/test pin (set by scripts/trace_chat.py's --sch). When non-None,
    # `load_active_schematic` reads/writes ONLY this file and skips live KiCad
    # detection — without it, detect() resolves to whatever project KiCad has
    # open and commit_edit would write the user's REAL .kicad_sch. This is the
    # mechanism behind --sch's documented "deterministic schematic context".
    forced_sch_path: Optional[Path] = None
    project_name: Optional[str] = None
    design_graph: Optional["DesignGraph"] = None
    profiles_by_mpn: dict[str, "ComponentProfile"] = field(default_factory=dict)
    # mtime of `active_sch_path` at the time `design_graph` was built;
    # `agent.schematic_sync.refresh_if_stale` reads stat().st_mtime each
    # turn and rebuilds when it has advanced.
    schematic_mtime: Optional[float] = None
    # Set when the user saved in KiCad after we staged an edit. The
    # digest header surfaces it so the model knows to discuss/discard
    # the stale stage instead of pretending the staged state is current.
    stage_stale: bool = False
    # Files the user attached in chat (PDFs etc.) keyed by attachment_id.
    # Populated by POST /agent/attachments; consumed by parse_datasheet.
    attachments: dict[str, "AttachmentRef"] = field(default_factory=dict)
    # parse_datasheet → design_spec handoff, keyed by MPN (mirrors
    # profiles_by_mpn; last-write-wins per MPN is fine for v1). parse_datasheet
    # stashes the resolved symbol + chosen variant; design_spec reads it,
    # runs the equation pass, and stashes back the synthesized netlist that
    # add_subcircuit_from_netlist then places.
    resolved_symbols: dict[str, dict] = field(default_factory=dict)
    design_specs: dict[str, "DesignSpec"] = field(default_factory=dict)
    pending_netlists: dict[str, dict] = field(default_factory=dict)
    # Generate-path handoff to the (next) wiring stage, keyed by the staged
    # .kicad_sch path (str). add_subcircuit_from_netlist stages the generate
    # path as a parts-only bin and retains the position-free netlist here so
    # the wiring stage can apply connectivity to the placed parts. Connectivity
    # is deferred, not discarded — this is where it lives in the meantime.
    staged_netlists: dict[str, dict] = field(default_factory=dict)
    # LLM routing for this conversation (provider + credential), set from the
    # chat/resume request headers by routes/agent.py. None = use settings/.env.
    # Stashed here so a suspended (ask_user) conversation resumes on the same
    # provider. See pinflow_api/llm.py.
    llm: Optional["LLMConfig"] = None
    # Live LLM-cost meter (credits this request / conversation), surfaced to the
    # UI via `cost` events and gating the per-request spend cap. See
    # pinflow_api/cost.py and the cost-metering block in agent/loop.py.
    cost: CostMeter = field(default_factory=CostMeter)


_lock = threading.Lock()
_states: dict[str, ConversationState] = {}


def get_or_create(conversation_id: str) -> ConversationState:
    with _lock:
        s = _states.get(conversation_id)
        if s is None:
            s = ConversationState(conversation_id=conversation_id)
            _states[conversation_id] = s
        return s


def get(conversation_id: str) -> Optional[ConversationState]:
    with _lock:
        return _states.get(conversation_id)

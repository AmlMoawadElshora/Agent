"""Conversation state machine, implemented as a LangGraph ``StateGraph``.

Graph shape::

    START -> extract -> guardrail ─┬─(stop / human)-> compose -> END
                                   └─(continue)-----> merge -> qualify -> compose -> END

* ``extract``   regex extraction (+ optional LLM gap-fill) of the 3 slots.
* ``guardrail`` opt-out / legal / negotiation / human-request detection.
* ``merge``     folds newly heard values into the lead's collected slots.
* ``qualify``   picks the next unanswered slot and enforces the attempt cap.
* ``compose``   writes the single next message (one question max).

Every node is a pure function of the state, which keeps the graph replayable
and lets the same code run without LangGraph installed (see ``run_turn``).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, TypedDict

from app.agent.extraction import extract_slots, merge_extractions
from app.agent.guardrails import evaluate as evaluate_guardrails
from app.agent.llm import get_llm_client
from app.agent.responder import compose_reply
from app.config import get_settings
from app.domain.enums import PropertyCondition
from app.domain.schemas import ExtractedSlots, LeadContext

logger = logging.getLogger(__name__)


class ConversationState(TypedDict, total=False):
    """Mutable state carried through one conversational turn."""

    # lead snapshot
    lead_id: str
    phone_number: str
    full_name: str | None
    status: str
    slot_hint: str | None        # the question we asked last
    already_handed_off: bool
    condition_raw: str | None
    # input
    message: str
    history: list[dict[str, str]]
    # state
    collected: dict[str, Any]
    questions_asked: dict[str, int]
    turn_count: int
    # working values
    extracted: dict[str, Any]
    guardrail: str | None
    opt_out: bool
    human_requested: bool
    next_slot: str | None
    qualified: bool
    escalated: bool
    no_new_info: bool
    handoff_required: bool
    # output
    reply: str
    message_kind: str


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def extract_node(state: ConversationState) -> dict[str, Any]:
    message = state.get("message", "")
    hint = state.get("slot_hint")
    slots = extract_slots(message, slot_hint=hint)

    llm = get_llm_client()
    if llm.enabled:
        # Only used to fill the gaps the rules could not resolve.
        slots = merge_extractions(slots, llm.extract_slots(message))

    return {"extracted": slots.model_dump(mode="json")}


def guardrail_node(state: ConversationState) -> dict[str, Any]:
    result = evaluate_guardrails(state.get("message", ""))
    guardrail = None
    if result.negotiation:
        guardrail = "price_negotiation"
    elif result.legal:
        guardrail = "legal"
    return {
        "opt_out": result.opt_out,
        "human_requested": result.human_requested,
        "guardrail": guardrail,
    }


def route_after_guardrail(state: ConversationState) -> Literal["alt_exit", "continue"]:
    if state.get("opt_out") or state.get("human_requested"):
        return "alt_exit"
    return "continue"


def merge_node(state: ConversationState) -> dict[str, Any]:
    extracted = ExtractedSlots.model_validate(state.get("extracted") or {})
    collected = dict(state.get("collected") or {})
    new_info = False

    if extracted.condition is not None:
        collected["condition"] = extracted.condition.value
        new_info = True
    if extracted.price_usd is not None:
        collected["price"] = extracted.price_usd
        new_info = True
    if extracted.size_sqft is not None:
        collected["size"] = extracted.size_sqft
        new_info = True

    update: dict[str, Any] = {"collected": collected, "no_new_info": not new_info}
    if extracted.condition_raw:
        update["condition_raw"] = extracted.condition_raw
    return update


def qualify_node(state: ConversationState) -> dict[str, Any]:
    settings = get_settings()
    order = settings.ordered_slots
    collected = state.get("collected") or {}

    next_slot = next((slot for slot in order if slot not in collected), None)
    qualified = next_slot is None

    questions = dict(state.get("questions_asked") or {})
    escalated = False
    if next_slot is not None:
        asked = int(questions.get(next_slot, 0))
        if asked >= settings.max_questions_per_slot:
            escalated = True
        else:
            questions[next_slot] = asked + 1

    return {
        "next_slot": next_slot,
        "qualified": qualified,
        "escalated": escalated,
        "questions_asked": questions,
        "handoff_required": qualified and not state.get("already_handed_off", False),
    }


def compose_node(state: ConversationState) -> dict[str, Any]:
    extracted = ExtractedSlots.model_validate(state.get("extracted") or {})
    collected = state.get("collected") or {}
    condition_raw = state.get("condition_raw") or extracted.condition_raw

    condition = None
    if collected.get("condition"):
        try:
            condition = PropertyCondition(collected["condition"])
        except ValueError:  # pragma: no cover - defensive
            condition = None

    context = LeadContext(
        lead_id=state.get("lead_id", ""),
        phone_number=state.get("phone_number", ""),
        full_name=state.get("full_name"),
        asking_price_usd=collected.get("price"),
        property_condition=condition,
        condition_raw=condition_raw,
        property_size_sqft=collected.get("size"),
        questions_asked=dict(state.get("questions_asked") or {}),
        turn_count=state.get("turn_count", 0),
    )

    qualified = bool(state.get("qualified"))
    already = bool(state.get("already_handed_off"))

    reply, kind = compose_reply(
        context,
        progress={},
        extracted=extracted,
        next_slot=state.get("next_slot"),
        questions_asked=dict(state.get("questions_asked") or {}),
        guardrail=state.get("guardrail"),
        qualified=qualified,
        just_qualified=(
            qualified
            and not already
            and not state.get("opt_out")
            and not state.get("human_requested")
        ),
        opted_out=bool(state.get("opt_out")),
        human_requested=bool(state.get("human_requested")),
        escalated=bool(state.get("escalated")),
        already_handed_off=already,
        llm=get_llm_client(),
    )
    return {"reply": reply, "message_kind": kind, "condition_raw": condition_raw}


# --------------------------------------------------------------------------- #
# Graph wiring
# --------------------------------------------------------------------------- #
def build_graph() -> Any:
    """Compile the LangGraph state machine (cached by the caller)."""
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(ConversationState)
    graph.add_node("extract", extract_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("merge", merge_node)
    graph.add_node("qualify", qualify_node)
    graph.add_node("compose", compose_node)

    graph.add_edge(START, "extract")
    graph.add_edge("extract", "guardrail")
    graph.add_conditional_edges(
        "guardrail",
        route_after_guardrail,
        {"alt_exit": "compose", "continue": "merge"},
    )
    graph.add_edge("merge", "qualify")
    graph.add_edge("qualify", "compose")
    graph.add_edge("compose", END)
    return graph.compile()


_compiled: Any | None = None


def _get_compiled() -> Any:
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


def reset_graph_cache() -> None:
    global _compiled
    _compiled = None


def run_turn(state: ConversationState) -> ConversationState:
    """Execute one turn through the LangGraph state machine."""
    merged: ConversationState = dict(state)  # type: ignore[assignment]
    merged.update(_get_compiled().invoke(state))
    return merged


def run_turn_deterministic(state: ConversationState) -> ConversationState:
    """Dependency-free execution path (used when LangGraph is unavailable).

    Runs exactly the same nodes in exactly the same order as the graph, so
    behaviour is identical; only the orchestration library differs.
    """
    merged: ConversationState = dict(state)  # type: ignore[assignment]
    merged.update(extract_node(merged))
    merged.update(guardrail_node(merged))
    if route_after_guardrail(merged) == "alt_exit":
        merged.update(compose_node(merged))
        return merged
    merged.update(merge_node(merged))
    merged.update(qualify_node(merged))
    merged.update(compose_node(merged))
    return merged

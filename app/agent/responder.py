"""Reply composition.

The deterministic composer below always produces a valid, on-policy message.
Optionally an LLM rewrites that message to sound more natural; the rewriter
receives the exact next question and can only rephrase.
"""

from __future__ import annotations

import logging

from app.agent import prompts
from app.agent.llm import LLMClient
from app.domain.enums import MessageKind
from app.domain.schemas import ExtractedSlots, LeadContext

logger = logging.getLogger(__name__)

# Order used to acknowledge several answers in one message.
_ANSWER_ORDER = ("price", "condition", "size")


def answered_slots(extracted: ExtractedSlots) -> list[str]:
    answered: list[str] = []
    if extracted.price_usd is not None:
        answered.append("price")
    if extracted.condition is not None:
        answered.append("condition")
    if extracted.size_sqft is not None:
        answered.append("size")
    return answered


def compose_greeting(context: LeadContext) -> str:
    """Opening outbound campaign message."""
    return prompts.greeting(context.full_name)


def compose_reply(
    context: LeadContext,
    *,
    progress: dict,
    extracted: ExtractedSlots,
    next_slot: str | None,
    questions_asked: dict[str, int],
    guardrail: str | None = None,
    qualified: bool = False,
    just_qualified: bool = False,
    opted_out: bool = False,
    human_requested: bool = False,
    escalated: bool = False,
    already_handed_off: bool = False,
    llm: LLMClient | None = None,
) -> tuple[str, str]:
    """Return ``(reply_text, message_kind)`` for one inbound turn."""
    del progress  # reserved for future personalisation hooks
    answered = answered_slots(extracted)

    # 1) Compliance first — stop talking immediately.
    if opted_out:
        return prompts.OPT_OUT_REPLY, MessageKind.OPT_OUT.value

    if human_requested:
        if qualified and context.asking_price_usd is not None:
            return (
                f"{prompts.qualification_complete(context.full_name)} "
                f"{prompts.HUMAN_HANDOFF_REPLY}",
                MessageKind.HUMAN_TAKEOVER.value,
            )
        return prompts.HUMAN_HANDOFF_REPLY, MessageKind.HUMAN_TAKEOVER.value

    # 2) Fully qualified this turn -> handoff confirmation.
    if just_qualified:
        return (
            prompts.qualification_complete(context.full_name),
            MessageKind.QUALIFICATION_COMPLETE.value,
        )

    if already_handed_off:
        return prompts.ALREADY_HANDED_OFF_REPLY, MessageKind.FOLLOW_UP.value

    # 3) Guardrail replies (never block collecting the remaining slots).
    prefix = ""
    kind = MessageKind.QUALIFICATION_QUESTION.value
    if guardrail == "price_negotiation":
        prefix = prompts.NEGOTIATION_REPLY
        kind = MessageKind.GUARDRAIL.value
    elif guardrail == "legal":
        prefix = prompts.LEGAL_REPLY
        kind = MessageKind.GUARDRAIL.value

    # 4) We ran out of polite attempts on the same question -> human.
    if escalated and next_slot is not None:
        return prompts.ESCALATION_REPLY, MessageKind.HUMAN_TAKEOVER.value

    # 5) Ask the next unanswered question (one at a time).
    if next_slot is not None:
        asked_before = questions_asked.get(next_slot, 1) - 1
        parts: list[str] = []
        if prefix:
            parts.append(prefix)
        if answered:
            ack_slot = next((s for s in _ANSWER_ORDER if s in answered), answered[0])
            parts.append(prompts.acknowledgement_for(ack_slot, asked_before))
        elif asked_before >= 1 and not prefix:
            parts.append(prompts.FOLLOW_UP_NUDGES.get(next_slot, ""))
        question = prompts.question_for(next_slot, asked_before)
        parts.append(question)
        reply = " ".join(part for part in parts if part).strip()
        if not prefix:
            kind = MessageKind.QUALIFICATION_QUESTION.value
        return _maybe_rewrite(context, reply, next_slot=next_slot, llm=llm), kind

    # 6) Nothing to ask and nothing understood.
    if prefix:
        return prefix, kind
    if qualified:
        return (
            prompts.qualification_complete(context.full_name),
            MessageKind.QUALIFICATION_COMPLETE.value,
        )
    return prompts.FALLBACK_REPLY, MessageKind.FOLLOW_UP.value


def _maybe_rewrite(
    context: LeadContext,
    reply: str,
    *,
    next_slot: str | None,
    llm: LLMClient | None,
) -> str:
    if llm is None or not llm.enabled:
        return reply
    try:
        rewritten = llm.rewrite_reply(context, reply, next_slot=next_slot)
    except Exception:  # noqa: BLE001 - the draft is always safe
        logger.exception("llm rewrite failed, using deterministic draft")
        return reply
    return rewritten or reply

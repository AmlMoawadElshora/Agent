"""End-to-end conversation behaviour (no HTTP, no Graph API, no LLM).

Runs the same scenarios twice: through LangGraph and through the
dependency-free execution path, so both orchestrators stay in lock-step.
"""

from __future__ import annotations

import pytest

from app.agent import prompts
from app.agent.engine import ConversationEngine, InboundOutcome
from app.db import repository
from app.db.base import session_scope
from app.domain.enums import HandoffStatus, LeadStatus, MessageKind, PropertyCondition
from app.services.whatsapp import SendResult
from tests.conftest import ADMIN_PHONE_E164

LEAD = "01020645168"
LEAD_E164 = "+11020645168"


@pytest.fixture(params=[True, False], ids=["langgraph", "deterministic"])
def use_graph(request) -> bool:
    return request.param


@pytest.fixture()
def engine(sender) -> ConversationEngine:
    return ConversationEngine(sender=sender)


def say(
    engine: ConversationEngine,
    body: str,
    *,
    sid: str | None = None,
    use_graph: bool = True,
) -> InboundOutcome:
    with session_scope() as db:
        return engine.handle_inbound(
            db, from_number=LEAD, body=body, provider_sid=sid, use_graph=use_graph
        )


def get_lead():
    with session_scope() as db:
        return repository.get_lead_by_phone(db, LEAD)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_full_qualification_and_handoff(engine, sender, use_graph) -> None:
    first = say(engine, "Hi, yes I'm still interested", use_graph=use_graph)
    assert "price" in first.reply.lower()
    assert first.qualified is False

    second = say(engine, "I'd like $450,000", use_graph=use_graph)
    assert "condition" in second.reply.lower()
    assert "turnkey" in second.reply.lower()

    third = say(engine, "It needs a full rehab", use_graph=use_graph)
    assert "square feet" in third.reply.lower()

    fourth = say(engine, "About 2,000 sq ft", use_graph=use_graph)
    assert fourth.qualified is True
    assert fourth.status == LeadStatus.HANDED_OFF.value
    assert fourth.handoff is not None and fourth.handoff.delivered is True

    lead = get_lead()
    assert lead.status == LeadStatus.HANDED_OFF.value
    assert lead.asking_price_usd == 450_000
    assert lead.property_condition == PropertyCondition.FULL_REHAB.value
    assert lead.property_size_sqft == 2_000
    assert lead.handoff_at is not None
    assert lead.turn_count == 4

    # Card went to the configured admin number with every required field.
    admin_messages = [m for m in sender.console.outbox if m["to"] == ADMIN_PHONE_E164]
    assert len(admin_messages) == 1
    card = admin_messages[0]["body"]
    assert "NEW QUALIFIED LEAD" in card
    assert LEAD_E164 in card
    assert "$450,000" in card
    assert "Full rehab" in card
    assert "2,000 sq ft" in card

    with session_scope() as db:
        handoff = repository.latest_handoff(db, lead.id)
        assert handoff is not None
        assert handoff.status == HandoffStatus.SENT.value
        assert handoff.admin_phone == ADMIN_PHONE_E164
        assert handoff.provider_sid


def test_each_question_is_asked_exactly_once(engine, sender, use_graph) -> None:
    replies = [
        say(engine, "yes", use_graph=use_graph).reply,
        say(engine, "450k", use_graph=use_graph).reply,
        say(engine, "turnkey", use_graph=use_graph).reply,
        say(engine, "1800", use_graph=use_graph).reply,
    ]

    questions = [
        prompts.QUESTIONS["price"][0],
        prompts.QUESTIONS["condition"][0],
        prompts.QUESTIONS["size"][0],
    ]
    for question in questions:
        occurrences = sum(reply.count(question) for reply in replies)
        assert occurrences == 1, f"question asked {occurrences} times: {question}"

    lead = get_lead()
    assert lead.status == LeadStatus.HANDED_OFF.value


def test_answers_out_of_order_are_not_re_asked(engine, sender, use_graph) -> None:
    # Lead volunteers condition and size before we ever ask.
    first = say(engine, "It's turnkey and about 2,100 sq ft", use_graph=use_graph)
    assert "price" in first.reply.lower()
    assert "condition" not in first.reply.lower()
    assert "square feet" not in first.reply.lower()

    second = say(engine, "$525k", use_graph=use_graph)
    assert second.qualified is True

    lead = get_lead()
    assert lead.asking_price_usd == 525_000
    assert lead.property_size_sqft == 2_100
    assert lead.property_condition == PropertyCondition.TURNKEY.value


def test_all_slots_in_one_message_qualifies_immediately(engine, sender, use_graph) -> None:
    outcome = say(
        engine,
        "Sure — asking $399,000, it's a full rehab, roughly 1,500 square feet.",
        use_graph=use_graph,
    )
    assert outcome.qualified is True
    assert outcome.status == LeadStatus.HANDED_OFF.value

    lead = get_lead()
    assert lead.asking_price_usd == 399_000
    assert lead.property_size_sqft == 1_500
    assert lead.property_condition == PropertyCondition.FULL_REHAB.value



# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #
def test_price_negotiation_is_refused_but_qualification_continues(
    engine, sender, use_graph
) -> None:
    say(engine, "yes still interested", use_graph=use_graph)
    outcome = say(
        engine,
        "Can you do better on price? What's your best offer?",
        use_graph=use_graph,
    )

    assert "not able to negotiate" in outcome.reply.lower()
    assert "?" in outcome.reply  # still asks for the price
    assert outcome.status == LeadStatus.IN_PROGRESS.value


def test_legal_question_is_deflected(engine, sender, use_graph) -> None:
    say(engine, "yes", use_graph=use_graph)
    outcome = say(
        engine,
        "Is this legally binding? Should I talk to an attorney?",
        use_graph=use_graph,
    )
    assert "legal" in outcome.reply.lower()
    assert outcome.status != LeadStatus.HANDED_OFF.value


def test_opt_out_stops_all_messaging(engine, sender, use_graph) -> None:
    say(engine, "yes", use_graph=use_graph)
    opt_out = say(engine, "Stop messaging me", use_graph=use_graph)

    assert opt_out.status == LeadStatus.OPTED_OUT.value
    assert "off our list" in opt_out.reply.lower()

    lead = get_lead()
    assert lead.opt_out is True

    outbox_before = len(sender.console.outbox)
    ignored = say(engine, "actually I changed my mind", use_graph=use_graph)
    assert ignored.ignored is True
    assert ignored.reply == ""
    assert len(sender.console.outbox) == outbox_before


def test_human_request_is_acknowledged(engine, sender, use_graph) -> None:
    say(engine, "yes", use_graph=use_graph)
    outcome = say(engine, "Can you call me?", use_graph=use_graph)

    assert "reach out to you directly" in outcome.reply.lower()
    lead = get_lead()
    assert "human_requested_at" in (lead.lead_metadata or {})


def test_repeated_unclear_answers_escalate_instead_of_looping(engine, sender, use_graph) -> None:
    say(engine, "yes", use_graph=use_graph)
    replies = [say(engine, "hmm not sure", use_graph=use_graph).reply for _ in range(3)]

    assert "follow up with you directly" in replies[-1].lower()
    lead = get_lead()
    # Never qualified, never handed off, still owned by a human pipeline.
    assert lead.status == LeadStatus.IN_PROGRESS.value
    assert lead.questions_asked["price"] >= 3


def test_fallback_for_media_only_message(engine, sender, use_graph) -> None:
    with session_scope() as db:
        outcome = engine.handle_inbound(
            db, from_number=LEAD, body="", provider_sid=None, num_media=1
        )
    assert "attachments" in outcome.reply.lower()



# --------------------------------------------------------------------------- #
# Idempotency / lifecycle
# --------------------------------------------------------------------------- #
def test_duplicate_webhook_is_ignored(engine, sender, use_graph) -> None:
    sid = "SMduplicate123"
    first = say(engine, "yes still interested", sid=sid, use_graph=use_graph)
    assert first.duplicate is False

    duplicate = say(engine, "yes still interested", sid=sid, use_graph=use_graph)
    assert duplicate.duplicate is True
    assert duplicate.reply == ""

    lead = get_lead()
    assert lead.turn_count == 1


def test_follow_up_after_handoff_does_not_re_handoff(engine, sender, use_graph) -> None:
    for message in ("yes", "$450k", "turnkey", "2000 sq ft"):
        say(engine, message, use_graph=use_graph)

    outbox_after_handoff = len(sender.console.outbox)
    outcome = say(engine, "Thanks! Looking forward to it.", use_graph=use_graph)

    assert outcome.qualified is True
    assert "already has your details" in outcome.reply.lower()
    admin_cards = [m for m in sender.console.outbox if m["to"] == ADMIN_PHONE_E164]
    assert len(admin_cards) == 1
    assert len(sender.console.outbox) == outbox_after_handoff + 1


def test_handoff_retries_after_transport_failure(engine, sender, use_graph) -> None:
    """If the admin card fails, the next inbound turn retries it."""

    class FlakySender:
        dry_run = True

        def __init__(self) -> None:
            self.fail_next = False
            self.outbox: list[dict[str, str]] = []

        def send_whatsapp(self, to: str, body: str) -> SendResult:
            if self.fail_next and to == ADMIN_PHONE_E164:
                self.fail_next = False
                return SendResult(ok=False, error="simulated graph api outage")
            self.outbox.append({"to": to, "body": body, "sid": f"SM{len(self.outbox)}"})
            return SendResult(ok=True, sid=f"SM{len(self.outbox)}", status="sent")

    flaky = FlakySender()
    engine.sender = flaky  # type: ignore[assignment]

    say(engine, "yes")
    say(engine, "$450k")
    say(engine, "turnkey")

    flaky.fail_next = True
    final = say(engine, "2000 sq ft")
    assert final.qualified is True
    assert final.handoff is not None and final.handoff.delivered is False

    lead = get_lead()
    assert lead.status == LeadStatus.QUALIFIED.value
    with session_scope() as db:
        failed = repository.latest_handoff(db, lead.id)
        assert failed is not None and failed.status == HandoffStatus.FAILED.value

    retry = say(engine, "hello?")
    assert retry.handoff is not None and retry.handoff.delivered is True

    lead = get_lead()
    assert lead.status == LeadStatus.HANDED_OFF.value
    with session_scope() as db:
        latest = repository.latest_handoff(db, lead.id)
        assert latest is not None and latest.status == HandoffStatus.SENT.value


def test_transcript_records_both_directions(engine, sender, use_graph) -> None:
    say(engine, "yes", use_graph=use_graph)
    lead = get_lead()

    with session_scope() as db:
        messages = repository.transcript(db, lead.id)
    assert [m.direction for m in messages] == ["INBOUND", "OUTBOUND"]
    assert messages[1].kind == MessageKind.QUALIFICATION_QUESTION.value

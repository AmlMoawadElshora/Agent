"""Handoff service: card contents, idempotency and failure semantics."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db import repository
from app.db.base import session_scope
from app.domain.enums import HandoffStatus, LeadStatus, PropertyCondition
from app.domain.schemas import SummaryCard
from app.services.handoff import NotQualifiedError, build_summary, perform_handoff
from app.services.whatsapp import SendResult
from tests.conftest import ADMIN_PHONE_E164

LEAD = "+14155550123"


@pytest.fixture()
def qualified_lead(sender):
    with session_scope() as db:
        lead, _ = repository.get_or_create_lead(
            db, LEAD, full_name="Dana Whitfield", source="county_records"
        )
        lead.asking_price_usd = 685_000
        lead.property_condition = PropertyCondition.MINOR_REPAIRS.value
        lead.condition_raw = "minor cosmetic repairs"
        lead.property_size_sqft = 2_450
        lead.turn_count = 5
        lead.status = LeadStatus.QUALIFIED.value
        db.commit()
        db.refresh(lead)
        lead_id = lead.id
    return lead_id


def test_build_summary_requires_all_slots(sender) -> None:
    with session_scope() as db:
        lead, _ = repository.get_or_create_lead(db, LEAD)
        lead.asking_price_usd = 500_000
        db.commit()
        db.refresh(lead)
        with pytest.raises(NotQualifiedError):
            build_summary(lead)


def test_summary_card_contains_required_fields(qualified_lead) -> None:
    with session_scope() as db:
        lead = repository.get_lead(db, qualified_lead)
        card = build_summary(lead)
        text = card.to_whatsapp_text()

    assert isinstance(card, SummaryCard)
    assert "*Name:* Dana Whitfield" in text
    assert f"*Phone:* {LEAD}" in text
    assert "*Asking price:* $685,000" in text
    assert "Minor cosmetic repairs" in text
    assert "*Size:* 2,450 sq ft" in text
    assert "*Lead ID:*" in text
    assert text.index("*Phone:*") < text.index("*Asking price:*")


def test_perform_handoff_sends_to_admin_once(qualified_lead, sender) -> None:
    with session_scope() as db:
        lead = repository.get_lead(db, qualified_lead)
        outcome = perform_handoff(db, lead, sender=sender)
        db.commit()

        assert outcome.delivered is True
        assert lead.status == LeadStatus.HANDED_OFF.value
        first_sid = outcome.handoff.provider_sid

    cards = [m for m in sender.console.outbox if m["to"] == ADMIN_PHONE_E164]
    assert len(cards) == 1
    assert "NEW QUALIFIED LEAD" in cards[0]["body"]
    assert "+14155550123" in cards[0]["body"]

    # Second call is a no-op (idempotent).
    with session_scope() as db:
        lead = repository.get_lead(db, qualified_lead)
        again = perform_handoff(db, lead, sender=sender)
        db.commit()
        assert again.delivered is True
        assert again.handoff.provider_sid == first_sid

    assert len([m for m in sender.console.outbox if m["to"] == ADMIN_PHONE_E164]) == 1


class _FailingSender:
    dry_run = False

    def __init__(self) -> None:
        self.calls = 0

    def send_whatsapp(self, to: str, body: str) -> SendResult:
        self.calls += 1
        return SendResult(ok=False, error="twilio 21610: recipient not joined")


def test_failed_handoff_keeps_lead_qualified_for_retry(qualified_lead) -> None:
    failing = _FailingSender()
    with session_scope() as db:
        lead = repository.get_lead(db, qualified_lead)
        outcome = perform_handoff(db, lead, sender=failing)
        db.commit()

        assert outcome.delivered is False
        assert "21610" in (outcome.error or "")
        assert lead.status == LeadStatus.QUALIFIED.value
        handoff = repository.latest_handoff(db, lead.id)
        assert handoff.status == HandoffStatus.FAILED.value
        assert handoff.error

    assert failing.calls == 1


def test_handoff_payload_is_structured(qualified_lead, sender) -> None:
    with session_scope() as db:
        lead = repository.get_lead(db, qualified_lead)
        outcome = perform_handoff(db, lead, sender=sender, notes="Called twice, motivated")
        db.commit()
        payload = outcome.handoff.payload

    assert payload["asking_price_usd"] == 685_000
    assert payload["property_size_sqft"] == 2_450
    assert payload["property_condition"] == PropertyCondition.MINOR_REPAIRS.value
    assert payload["notes"] == "Called twice, motivated"
    assert "card_text" in payload
    assert payload["qualified_at"]


def test_qualified_at_uses_utc(qualified_lead) -> None:
    with session_scope() as db:
        lead = repository.get_lead(db, qualified_lead)
        card = build_summary(lead)
    assert card.qualified_at.tzinfo is not None or card.qualified_at <= datetime.now(UTC)

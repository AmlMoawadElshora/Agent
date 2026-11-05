"""Twilio webhook integration tests (signature, TwiML, dedupe, handoff)."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from app.db import repository
from app.db.base import session_scope
from app.domain.enums import HandoffStatus, LeadStatus, MessageDirection
from tests.conftest import (
    ADMIN_PHONE_E164,
    LEAD_PHONE_E164,
    WEBHOOK_PATH,
    post_inbound,
    sign_twilio_request,
)

WEBHOOK = WEBHOOK_PATH


def test_probe_endpoint_is_public(client) -> None:
    response = client.get(WEBHOOK)
    assert response.status_code == 200
    assert "webhook" in response.json()["message"].lower()


def test_inbound_message_returns_twiml(client, sender) -> None:
    response, reply = post_inbound(client, "Hi yes, still interested")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "price" in reply.lower()

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        assert lead is not None
        assert lead.status == LeadStatus.IN_PROGRESS.value
        assert lead.turn_count == 1

        messages = repository.transcript(db, lead.id)
        assert [m.direction for m in messages] == ["INBOUND", "OUTBOUND"]
        assert messages[0].provider_sid
        assert messages[1].kind == "QUALIFICATION_QUESTION"


def test_invalid_signature_is_rejected(client, sender) -> None:
    response, _ = post_inbound(client, "hello", signature="deadbeef", sign=True)
    assert response.status_code == 403

    response, _ = post_inbound(client, "hello", sign=False)
    assert response.status_code == 403

    with session_scope() as db:
        assert repository.get_lead_by_phone(db, LEAD_PHONE_E164) is None


def test_duplicate_message_sid_is_ignored(client, sender) -> None:
    first, _ = post_inbound(client, "yes interested", sid="SMdup999")
    second, second_reply = post_inbound(client, "yes interested", sid="SMdup999")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second_reply == ""

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        assert lead.turn_count == 1
        assert len(repository.transcript(db, lead.id)) == 2  # 1 in + 1 out



def test_status_callback_updates_delivery_state(client, sender) -> None:
    post_inbound(client, "yes", sid="SMstatus1")

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        message = repository.add_message(
            db,
            lead,
            direction=MessageDirection.OUTBOUND,
            body="test",
            provider_sid="SMoutbound1",
            delivery_status="queued",
        )
        message_id = message.id

    form = {
        "From": "whatsapp:+14155238886",
        "To": "whatsapp:01020645168",
        "MessageSid": "SMoutbound1",
        "MessageStatus": "delivered",
        "SmsStatus": "delivered",
    }
    response = client.post(
        WEBHOOK_PATH,
        data=form,
        headers={
            "X-Twilio-Signature": sign_twilio_request(
                f"http://testserver{WEBHOOK_PATH}", form
            )
        },
    )
    assert response.status_code == 204

    with session_scope() as db:
        from app.db.models import Message

        stored = db.get(Message, message_id)
        assert stored is not None
        assert stored.delivery_status == "delivered"


def test_full_conversation_over_webhook_triggers_handoff(client, sender) -> None:
    script = [
        ("Hi, yes I'm still interested", "price"),
        ("Looking for $450,000", "condition"),
        ("It needs a full rehab", "square"),
        ("About 2,000 sq ft", "everything i need"),
    ]
    for message, expected in script:
        response, reply = post_inbound(client, message)
        assert response.status_code == 200
        assert expected in reply.lower(), reply

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        assert lead.status == LeadStatus.HANDED_OFF.value
        assert lead.asking_price_usd == 450_000
        assert lead.property_size_sqft == 2_000

        handoff = repository.latest_handoff(db, lead.id)
        assert handoff is not None
        assert handoff.status == HandoffStatus.SENT.value
        assert handoff.admin_phone == ADMIN_PHONE_E164

    admin_cards = [m for m in sender.console.outbox if m["to"] == ADMIN_PHONE_E164]
    assert len(admin_cards) == 1
    card = admin_cards[0]["body"]
    assert LEAD_PHONE_E164 in card
    assert "$450,000" in card
    assert "2,000 sq ft" in card


def test_opted_out_lead_gets_no_further_messages(client, sender) -> None:
    post_inbound(client, "yes")
    _, reply = post_inbound(client, "STOP")
    assert "off our list" in reply.lower()

    _, follow_up = post_inbound(client, "hello?")
    assert follow_up == ""

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        assert lead.opt_out is True
        assert lead.status == LeadStatus.OPTED_OUT.value


def test_unparseable_from_number_is_acknowledged_quietly(client, sender) -> None:
    form = {
        "From": "whatsapp:abc",
        "Body": "hello",
        "MessageSid": "SMbadphone",
        "NumMedia": "0",
    }
    response = client.post(
        WEBHOOK_PATH,
        content=urlencode(form),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": sign_twilio_request(
                f"http://testserver{WEBHOOK_PATH}", form
            ),
        },
    )
    assert response.status_code == 200
    assert "<Message>" not in response.text


@pytest.mark.parametrize("body", ["I want to speak to a human", "can you call me"])
def test_human_request_is_routed_to_team(client, sender, body: str) -> None:
    post_inbound(client, "yes")
    _, reply = post_inbound(client, body)
    assert "reach out to you directly" in reply.lower()

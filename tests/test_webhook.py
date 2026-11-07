"""Meta Cloud API webhook integration tests.

Covers the verification handshake, ``X-Hub-Signature-256`` enforcement, JSON
message parsing (text/button/interactive/media), delivery receipts, duplicate
``wamid`` handling, and the end-to-end qualification + handoff flow.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db import repository
from app.db.base import session_scope
from app.db.models import Message
from app.domain.enums import HandoffStatus, LeadStatus, MessageDirection
from tests.conftest import (
    ADMIN_PHONE_E164,
    LEAD_PHONE_E164,
    META_VERIFY_TOKEN,
    WEBHOOK_PATH,
    post_inbound,
    post_status,
    replies_to,
    sign_meta_payload,
)

WEBHOOK = WEBHOOK_PATH


# --------------------------------------------------------------------------- #
# Verification handshake
# --------------------------------------------------------------------------- #
def test_verification_handshake_echoes_challenge(client) -> None:
    response = client.get(
        WEBHOOK,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": META_VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 200
    assert response.text == "1158201444"
    assert response.headers["content-type"].startswith("text/plain")


def test_verification_rejects_wrong_token(client) -> None:
    response = client.get(
        WEBHOOK,
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "123"},
    )
    assert response.status_code == 403


def test_verification_rejects_unsupported_mode(client) -> None:
    response = client.get(
        WEBHOOK,
        params={"hub.mode": "unsubscribe", "hub.verify_token": META_VERIFY_TOKEN},
    )
    assert response.status_code == 400


def test_health_reports_meta_configuration(client) -> None:
    payload = client.get("/health").json()
    assert payload["meta_configured"] is True
    assert payload["dry_run"] is True


# --------------------------------------------------------------------------- #
# Signature enforcement
# --------------------------------------------------------------------------- #
def test_invalid_signature_is_rejected(client, sender) -> None:
    response, _ = post_inbound(client, "hello", signature="sha256=deadbeef", sign=True)
    assert response.status_code == 403

    response, _ = post_inbound(client, "hello", sign=False)
    assert response.status_code == 403

    with session_scope() as db:
        assert repository.get_lead_by_phone(db, LEAD_PHONE_E164) is None


# --------------------------------------------------------------------------- #
# Message handling
# --------------------------------------------------------------------------- #
def test_inbound_message_is_processed_and_replied(client, sender) -> None:
    response, reply = post_inbound(client, "Hi yes, still interested")

    assert response.status_code == 200
    assert "price" in reply.lower()

    # No TwiML/XML is returned any more — the reply goes out over the Graph API.
    assert "<Response>" not in response.text

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        assert lead is not None
        assert lead.status == LeadStatus.IN_PROGRESS.value
        assert lead.turn_count == 1

        messages = repository.transcript(db, lead.id)
        assert [m.direction for m in messages] == ["INBOUND", "OUTBOUND"]
        assert messages[0].provider_sid.startswith("wamid.")
        assert messages[1].kind == "QUALIFICATION_QUESTION"
        assert messages[1].provider_sid  # dry-run id recorded for traceability
        assert messages[1].message_metadata["provider"] == "meta_cloud_api"


def test_duplicate_wamid_is_ignored(client, sender) -> None:
    first, _ = post_inbound(client, "yes interested", wamid="wamid.dup-999")
    second, second_reply = post_inbound(client, "yes interested", wamid="wamid.dup-999")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second_reply == ""
    assert len(replies_to()) == 1

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        assert lead.turn_count == 1
        assert len(repository.transcript(db, lead.id)) == 2  # 1 in + 1 out


def test_delivery_receipt_updates_message_status(client, sender) -> None:
    post_inbound(client, "yes", wamid="wamid.status-1")

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        outbound = repository.add_message(
            db,
            lead,
            direction=MessageDirection.OUTBOUND,
            body="test",
            provider_sid="wamid.outbound-1",
            delivery_status="sent",
        )
        outbound_id = outbound.id

    response = post_status(client, "wamid.outbound-1", "read")
    assert response.status_code == 200

    with session_scope() as db:
        stored = db.get(Message, outbound_id)
        assert stored is not None
        assert stored.delivery_status == "read"


def test_failed_receipt_records_error(client, sender) -> None:
    post_inbound(client, "yes", wamid="wamid.status-2")

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        repository.add_message(
            db,
            lead,
            direction=MessageDirection.OUTBOUND,
            body="test",
            provider_sid="wamid.outbound-2",
            delivery_status="sent",
        )

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "99887766",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.outbound-2",
                                    "status": "failed",
                                    "timestamp": "1735689600",
                                    "errors": [
                                        {
                                            "code": 131047,
                                            "title": "Re-engagement message",
                                            "error_data": {"details": "24h window closed"},
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload).encode()
    response = client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_meta_payload(raw),
        },
    )
    assert response.status_code == 200

    with session_scope() as db:
        stored = db.scalar(select(Message).where(Message.provider_sid == "wamid.outbound-2"))
        assert stored is not None
        assert stored.delivery_status == "failed"
        assert stored.message_metadata["delivery_error"]["code"] == 131047


def test_status_for_unknown_message_is_ignored(client, sender) -> None:
    response = post_status(client, "wamid.unknown", "delivered")
    assert response.status_code == 200


def test_button_and_interactive_replies_are_understood(client, sender) -> None:
    post_inbound(client, "yes", wamid="wamid.flow-1")

    _, button_reply = post_inbound(
        client, "It's turnkey", wamid="wamid.flow-2", message_type="button"
    )
    assert "number" in button_reply.lower() or "price" in button_reply.lower()

    _, interactive_reply = post_inbound(
        client, "$450,000", wamid="wamid.flow-3", message_type="interactive"
    )
    assert "square" in interactive_reply.lower()

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        assert lead.property_condition == "TURNKEY"
        assert lead.asking_price_usd == 450_000


def test_media_message_asks_for_text(client, sender) -> None:
    _, reply = post_inbound(client, "", wamid="wamid.media-1", message_type="image")
    assert "attachments" in reply.lower()


def test_unknown_payload_object_is_acknowledged(client, sender) -> None:
    raw = json.dumps({"object": "page", "entry": []}).encode()
    response = client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_meta_payload(raw),
        },
    )
    assert response.status_code == 200
    assert replies_to() == []


def test_malformed_json_is_rejected(client, sender) -> None:
    raw = b"not-json"
    response = client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_meta_payload(raw),
        },
    )
    assert response.status_code == 400


def test_unparseable_sender_is_ignored(client, sender) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "99887766",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"profile": {"name": "Bad"}, "wa_id": "abc"}],
                            "messages": [
                                {
                                    "from": "abc",
                                    "id": "wamid.bad-number",
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload).encode()
    response = client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_meta_payload(raw),
        },
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #
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
    post_inbound(client, "yes", wamid="wamid.opt-1")
    _, reply = post_inbound(client, "STOP", wamid="wamid.opt-2")
    assert "off our list" in reply.lower()

    replies_before = len(replies_to())
    _, follow_up = post_inbound(client, "hello?", wamid="wamid.opt-3")
    assert follow_up == ""
    assert len(replies_to()) == replies_before

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, LEAD_PHONE_E164)
        assert lead.opt_out is True
        assert lead.status == LeadStatus.OPTED_OUT.value


@pytest.mark.parametrize("body", ["I want to speak to a human", "can you call me"])
def test_human_request_is_routed_to_team(client, sender, body: str) -> None:
    post_inbound(client, "yes", wamid=f"wamid.hr-{abs(hash(body))}-1")
    _, reply = post_inbound(client, body, wamid=f"wamid.hr-{abs(hash(body))}-2")
    assert "reach out to you directly" in reply.lower()


def test_webhook_does_not_leak_twilio_style_form_fields(client, sender) -> None:
    """Form-encoded posts must no longer be accepted as WhatsApp messages."""
    response = client.post(
        WEBHOOK,
        data={"From": "whatsapp:01020645168", "Body": "hi", "MessageSid": "SM123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # Signature missing first -> rejected; either way the message is not handled.
    assert response.status_code in {400, 403}
    assert replies_to() == []

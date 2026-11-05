"""Admin REST API tests: auth, lead CRUD, campaign launch, handoff."""

from __future__ import annotations

from app.db import repository
from app.db.base import session_scope
from app.domain.enums import LeadStatus, PropertyCondition
from tests.conftest import ADMIN_PHONE_E164, LEAD_PHONE_E164


def create_lead(client, headers, phone: str = "01020645168", name: str = "John Smith"):
    return client.post(
        "/api/leads",
        json={"phone_number": phone, "full_name": name, "source": "csv_import"},
        headers=headers,
    )


def test_admin_api_requires_key(client) -> None:
    assert client.get("/api/leads").status_code == 401
    assert client.get("/api/leads", headers={"X-API-Key": "wrong"}).status_code == 401


def test_health_endpoints_are_open(client) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "ok"
    assert payload["dry_run"] is True
    assert payload["admin_number_configured"] is True

    assert client.get("/ready").json() == {"status": "ready"}


def test_create_lead_normalises_phone(client, admin_headers) -> None:
    response = create_lead(client, admin_headers)
    assert response.status_code == 201
    body = response.json()
    assert body["phone_number"] == LEAD_PHONE_E164
    assert body["status"] == LeadStatus.NEW.value
    assert body["full_name"] == "John Smith"


def test_create_lead_is_idempotent(client, admin_headers) -> None:
    first = create_lead(client, admin_headers).json()
    second = create_lead(client, admin_headers).json()
    assert first["id"] == second["id"]


def test_invalid_phone_returns_422(client, admin_headers) -> None:
    response = create_lead(client, admin_headers, phone="not-a-phone")
    assert response.status_code == 422


def test_bulk_import(client, admin_headers) -> None:
    response = client.post(
        "/api/leads/bulk",
        json={
            "leads": [
                {"phone_number": "01020645167", "full_name": "A"},
                {"phone_number": "01131043304", "full_name": "B"},
                {"phone_number": "01032547715", "full_name": "C"},
            ]
        },
        headers=admin_headers,
    )
    assert response.status_code == 201
    assert len(response.json()) == 3

    listed = client.get("/api/leads?limit=10", headers=admin_headers).json()
    assert {lead["phone_number"] for lead in listed} == {
        "+11020645167",
        "+11131043304",
        "+11032547715",
    }


def test_list_filter_and_404(client, admin_headers) -> None:
    create_lead(client, admin_headers)
    assert client.get("/api/leads?status=NEW", headers=admin_headers).json() != []
    assert client.get("/api/leads?status=HANDED_OFF", headers=admin_headers).json() == []
    assert client.get("/api/leads/does-not-exist", headers=admin_headers).status_code == 404


def test_campaign_launch_sends_openers(client, sender, admin_headers) -> None:
    response = client.post(
        "/api/campaigns/launch",
        json={
            "leads": [
                {"phone_number": "01020645167", "full_name": "Alice"},
                {"phone_number": "01131043304"},
            ],
            "dry_run": True,
            "source": "api_campaign",
        },
        headers=admin_headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["sent"] == 2
    assert payload["failed"] == 0
    assert payload["dry_run"] is True

    openers = [m for m in sender.console.outbox if "cash offer" in m["body"]]
    assert len(openers) == 2

    with session_scope() as db:
        lead = repository.get_lead_by_phone(db, "+11020645167")
        assert lead.status == LeadStatus.CONTACTED.value

    # Re-running the campaign must not message the same person again.
    again = client.post(
        "/api/campaigns/launch",
        json={"leads": [{"phone_number": "01020645167"}], "dry_run": True},
        headers=admin_headers,
    ).json()
    assert again["skipped"] == 1
    assert again["sent"] == 0


def test_campaign_rejects_invalid_numbers(client, sender, admin_headers) -> None:
    response = client.post(
        "/api/campaigns/launch",
        json={"leads": [{"phone_number": "abc"}], "dry_run": True},
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert sender.console.outbox == []


def test_manual_message_and_transcript(client, sender, admin_headers) -> None:
    lead = create_lead(client, admin_headers).json()

    response = client.post(
        f"/api/leads/{lead['id']}/messages",
        json={"body": "Hi John, this is the acquisitions manager."},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["direction"] == "OUTBOUND"
    assert response.json()["kind"] == "HUMAN_TAKEOVER"

    transcript = client.get(f"/api/leads/{lead['id']}/messages", headers=admin_headers).json()
    assert len(transcript) == 1
    assert "acquisitions manager" in transcript[0]["body"]
    assert sender.console.outbox[-1]["to"] == LEAD_PHONE_E164


def test_handoff_requires_qualification_then_succeeds(client, sender, admin_headers) -> None:
    lead = create_lead(client, admin_headers).json()

    response = client.post(f"/api/leads/{lead['id']}/handoff", headers=admin_headers)
    assert response.status_code == 409
    assert "qualification" in response.json()["detail"].lower()

    # Simulate the slots having been collected.
    with session_scope() as db:
        stored = repository.get_lead(db, lead["id"])
        stored.asking_price_usd = 450_000
        stored.property_condition = PropertyCondition.TURNKEY.value
        stored.property_size_sqft = 2_000
        stored.status = LeadStatus.QUALIFIED.value
        repository.apply_context(db, stored, repository.to_context(stored))
        db.commit()

    response = client.post(f"/api/leads/{lead['id']}/handoff", headers=admin_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["delivered"] is True
    assert payload["summary"]["asking_price_usd"] == 450_000
    assert payload["summary"]["property_size_sqft"] == 2_000

    cards = [m for m in sender.console.outbox if m["to"] == ADMIN_PHONE_E164]
    assert len(cards) == 1
    assert "+11020645168" in cards[0]["body"]

    listed = client.get("/api/handoffs", headers=admin_headers).json()
    assert len(listed) == 1
    assert listed[0]["status"] == "SENT"


def test_opt_out_endpoint(client, admin_headers) -> None:
    lead = create_lead(client, admin_headers).json()
    response = client.post(f"/api/leads/{lead['id']}/opt-out", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == LeadStatus.OPTED_OUT.value
    assert response.json()["opt_out"] is True


def test_stats_endpoint(client, admin_headers) -> None:
    create_lead(client, admin_headers)
    payload = client.get("/api/stats", headers=admin_headers).json()
    assert payload["total_leads"] == 1
    assert payload["by_status"]["NEW"] == 1
    assert payload["conversion_rate"] == 0.0

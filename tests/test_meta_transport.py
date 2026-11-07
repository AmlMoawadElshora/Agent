"""Unit tests for the Meta WhatsApp Cloud API transport.

Uses ``httpx.MockTransport`` so no network call is ever made, and asserts the
exact request Meta expects (URL, Bearer token, JSON body) plus error handling.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.services.whatsapp import (
    ConsoleSender,
    MetaWhatsAppSender,
    SenderRouter,
    render_template_parameters,
)

PHONE_ID = "1029384756"
GRAPH_URL = f"https://graph.facebook.com/v18.0/{PHONE_ID}/messages"


def make_settings(**overrides) -> Settings:
    base = {
        "meta_access_token": "EAAG-test-token",
        "meta_phone_number_id": PHONE_ID,
        "meta_app_secret": "secret",
        "meta_welcome_template_name": "real_estate_opener",
        "meta_template_language": "en_US",
        "meta_template_params": "first_name,company",
        "meta_max_retries": 0,
        "dry_run": False,
        "admin_whatsapp_number": "01020645167",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def sender_with(handler) -> tuple[MetaWhatsAppSender, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    client = httpx.Client(
        transport=httpx.MockTransport(record),
        headers={
            "Authorization": "Bearer EAAG-test-token",
            "Content-Type": "application/json",
        },
    )
    return MetaWhatsAppSender(make_settings(), client=client), captured


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": "14155550123", "wa_id": "14155550123"}],
            "messages": [
                {"id": "wamid.HBgNMTQxNTU1NTAxMjM=", "message_status": "accepted"}
            ],
        },
    )


def test_text_message_payload() -> None:
    sender, captured = sender_with(ok_handler)

    result = sender.send_whatsapp("+1 (415) 555-0123", "Hello there")

    assert result.ok is True
    assert result.sid == "wamid.HBgNMTQxNTU1NTAxMjM="
    assert result.status == "accepted"

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == GRAPH_URL
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer EAAG-test-token"

    body = json.loads(request.content)
    assert body == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "14155550123",  # digits only, no '+'
        "type": "text",
        "text": {"preview_url": False, "body": "Hello there"},
    }


def test_template_message_payload_with_parameters() -> None:
    sender, captured = sender_with(ok_handler)

    result = sender.send_template(
        "01020645168",
        "real_estate_opener",
        "en_US",
        ["John"],
        fallback_text="ignored for live sends",
    )

    assert result.ok is True
    assert result.message_type == "template"

    body = json.loads(captured[0].content)
    assert body["type"] == "template"
    assert body["to"] == "11020645168"
    assert body["template"] == {
        "name": "real_estate_opener",
        "language": {"code": "en_US"},
        "components": [
            {"type": "body", "parameters": [{"type": "text", "text": "John"}]}
        ],
    }


def test_template_without_parameters_omits_components() -> None:
    sender, captured = sender_with(ok_handler)
    sender.send_template("14155550123", "plain_template")
    body = json.loads(captured[0].content)
    assert "components" not in body["template"]


def test_error_response_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "(#131047) Re-engagement message",
                    "type": "OAuthException",
                    "code": 131047,
                    "error_subcode": 2494010,
                    "fbtrace_id": "AbC123",
                }
            },
        )

    sender, _ = sender_with(handler)
    result = sender.send_whatsapp("14155550123", "hi")

    assert result.ok is False
    assert "meta 400" in result.error
    assert "code=131047" in result.error
    assert "Re-engagement message" in result.error
    assert "AbC123" in result.error
    assert result.status == "400"


def test_transient_error_is_retried() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, json={"error": {"message": "try again"}})
        return ok_handler(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = MetaWhatsAppSender(make_settings(meta_max_retries=1), client=client)

    result = sender.send_whatsapp("14155550123", "hi")

    assert attempts["count"] == 2
    assert result.ok is True


def test_permanent_error_is_not_retried() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(
            400, json={"error": {"message": "bad recipient", "code": 131026}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = MetaWhatsAppSender(make_settings(meta_max_retries=2), client=client)

    result = sender.send_whatsapp("14155550123", "hi")

    assert attempts["count"] == 1
    assert result.ok is False


def test_network_error_returns_result_not_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = MetaWhatsAppSender(make_settings(meta_max_retries=0), client=client)

    result = sender.send_whatsapp("14155550123", "hi")

    assert result.ok is False
    assert "ConnectError" in result.error


def test_settings_requires_credentials() -> None:
    with pytest.raises(RuntimeError):
        MetaWhatsAppSender(make_settings(meta_access_token=None))


def test_router_falls_back_to_dry_run_without_credentials() -> None:
    settings = make_settings(meta_access_token=None, meta_phone_number_id=None)
    router = SenderRouter(settings)

    assert router.dry_run is True
    result = router.send_whatsapp("01020645168", "hello")

    assert result.ok is True
    assert result.dry_run is True
    assert router.console.outbox[-1]["body"] == "hello"


def test_router_dry_run_records_template_metadata() -> None:
    router = SenderRouter(make_settings(dry_run=True))
    result = router.send_template(
        "01020645168",
        "real_estate_opener",
        "en_US",
        ["John"],
        fallback_text="Hi John, this is Alex...",
    )

    entry = router.console.outbox[-1]
    assert result.dry_run is True
    assert entry["type"] == "template"
    assert entry["template"] == "real_estate_opener"
    assert entry["parameters"] == ["John"]
    assert entry["body"].startswith("Hi John")


class _Lead:
    def __init__(self, full_name: str | None) -> None:
        self.full_name = full_name


def test_render_template_parameters_tokens() -> None:
    settings = make_settings(
        meta_template_params="first_name,full_name,company,market,agent"
    )
    params = render_template_parameters(settings, _Lead("John Smith"))

    assert params == [
        "John",
        "John Smith",
        "Summit Bridge Properties",
        "United States",
        "Alex",
    ]


def test_render_template_parameters_without_name() -> None:
    settings = make_settings(meta_template_params="first_name")
    assert render_template_parameters(settings, _Lead(None)) == ["there"]


def test_console_sender_never_calls_network() -> None:
    console = ConsoleSender()
    result = console.send_whatsapp("+14155550123", "hello")
    assert result.ok is True
    assert result.dry_run is True
    assert console.outbox[0]["to"] == "+14155550123"


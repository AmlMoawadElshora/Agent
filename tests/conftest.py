"""Shared pytest fixtures.

The suite runs entirely offline: SQLite in memory, console sender (no Graph API
calls), no LLM.  Every test gets a clean schema and a fresh dry-run outbox.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

# Must be set before the app package (and its cached settings) is imported.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": "sqlite://",
        "DRY_RUN": "true",
        "ADMIN_WHATSAPP_NUMBER": "01020645167",
        # Meta Cloud API credentials
        "META_ACCESS_TOKEN": "test-meta-token",
        "META_PHONE_NUMBER_ID": "1029384756",
        "META_WABA_ID": "99887766",
        "META_VERIFY_TOKEN": "test-verify-token",
        "META_APP_SECRET": "test-app-secret",
        "VALIDATE_META_SIGNATURE": "true",
        "META_WELCOME_TEMPLATE_NAME": "real_estate_opener",
        "META_TEMPLATE_LANGUAGE": "en_US",
        "META_TEMPLATE_PARAMS": "first_name",
        # app
        "PUBLIC_BASE_URL": "http://testserver",
        "ADMIN_API_KEY": "test-admin-key",
        "LLM_ENABLED": "false",
        "MAX_QUESTIONS_PER_SLOT": "3",
        "LOG_LEVEL": "WARNING",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app.agent.graph import reset_graph_cache  # noqa: E402
from app.agent.llm import reset_llm_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import get_engine, init_db, reset_state  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.domain.phones import normalize_phone, to_meta_recipient  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.locks import reset_locks  # noqa: E402
from app.services.whatsapp import SenderRouter, get_sender, set_sender  # noqa: E402

ADMIN_PHONE_E164 = "+11020645167"
LEAD_PHONE_E164 = "+11020645168"
LEAD_PHONE_LOCAL = "01020645168"
WEBHOOK_PATH = "/webhooks/whatsapp"
ADMIN_API_KEY = "test-admin-key"
META_APP_SECRET = "test-app-secret"
META_VERIFY_TOKEN = "test-verify-token"
META_PHONE_NUMBER_ID = "1029384756"


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    """Fresh schema for every test (in-memory DB is shared per process)."""
    get_settings.cache_clear()
    reset_state()
    reset_graph_cache()
    reset_llm_client()
    reset_locks()

    init_db()
    yield

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    reset_state()


@pytest.fixture()
def settings():
    return get_settings()


@pytest.fixture()
def sender(settings) -> Iterator[SenderRouter]:
    router = SenderRouter(settings)
    set_sender(router)
    yield router
    set_sender(None)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": ADMIN_API_KEY}


# --------------------------------------------------------------------------- #
# Meta Cloud API helpers
# --------------------------------------------------------------------------- #
def sign_meta_payload(raw_body: bytes, secret: str = META_APP_SECRET) -> str:
    """Build the ``X-Hub-Signature-256`` value Meta would send."""
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def inbound_payload(
    body: str,
    *,
    from_number: str = LEAD_PHONE_LOCAL,
    wamid: str | None = None,
    profile_name: str = "John Smith",
    message_type: str = "text",
    media_id: str = "media-123",
) -> dict[str, Any]:
    """A realistic Cloud API ``messages`` webhook payload."""
    mid = wamid or f"wamid.{uuid.uuid4().hex}"
    recipient = to_meta_recipient(from_number)

    message: dict[str, Any] = {
        "from": recipient,
        "id": mid,
        "timestamp": "1735689600",
        "type": message_type,
    }
    if message_type == "text":
        message["text"] = {"body": body}
    elif message_type == "image":
        message["image"] = {"id": media_id, "mime_type": "image/jpeg", "caption": body}
    elif message_type == "button":
        message["button"] = {"text": body, "payload": body}
    elif message_type == "interactive":
        message["interactive"] = {
            "type": "button_reply",
            "button_reply": {"id": "p1", "title": body},
        }

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "99887766",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "14155238886",
                                "phone_number_id": META_PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": profile_name},
                                    "wa_id": recipient,
                                }
                            ],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def status_payload(provider_sid: str, status: str = "delivered") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "99887766",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": META_PHONE_NUMBER_ID},
                            "statuses": [
                                {
                                    "id": provider_sid,
                                    "status": status,
                                    "timestamp": "1735689600",
                                    "recipient_id": to_meta_recipient(LEAD_PHONE_LOCAL),
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def replies_to(from_number: str = LEAD_PHONE_LOCAL) -> list[dict[str, Any]]:
    """Dry-run outbox entries addressed to ``from_number``."""
    target = normalize_phone(from_number)
    router = get_sender()
    console = getattr(router, "console", None)
    if console is None:
        return []
    return [m for m in console.outbox if m.get("to") == target]


def post_inbound(
    client: TestClient,
    body: str,
    *,
    from_number: str = LEAD_PHONE_LOCAL,
    wamid: str | None = None,
    profile_name: str = "John Smith",
    message_type: str = "text",
    signature: str | None = None,
    sign: bool = True,
) -> tuple[Any, str]:
    """POST a signed Meta webhook and return ``(response, last reply text)``."""
    payload = inbound_payload(
        body,
        from_number=from_number,
        wamid=wamid,
        profile_name=profile_name,
        message_type=message_type,
    )
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = signature or sign_meta_payload(raw)

    before = len(replies_to(from_number))
    response = client.post(WEBHOOK_PATH, content=raw, headers=headers)
    new_replies = replies_to(from_number)[before:]
    return response, (new_replies[-1]["body"] if new_replies else "")


def post_status(client: TestClient, provider_sid: str, status: str = "delivered"):
    raw = json.dumps(status_payload(provider_sid, status)).encode()
    return client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_meta_payload(raw),
        },
    )

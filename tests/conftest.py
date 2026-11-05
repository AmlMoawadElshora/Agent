"""Shared pytest fixtures.

The suite runs entirely offline: SQLite in memory, console sender (no Twilio),
no LLM.  Every test gets a clean schema and a fresh dry-run outbox.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import uuid
from collections.abc import Iterator
from urllib.parse import urlencode

import pytest

# Must be set before the app package (and its cached settings) is imported.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": "sqlite://",
        "DRY_RUN": "true",
        "ADMIN_WHATSAPP_NUMBER": "01020645167",
        "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
        "TWILIO_ACCOUNT_SID": "ACtest0000000000000000000000000000",
        "TWILIO_AUTH_TOKEN": "test-auth-token",
        "VALIDATE_TWILIO_SIGNATURE": "true",
        "PUBLIC_BASE_URL": "http://testserver",
        "ADMIN_API_KEY": "test-admin-key",
        "LLM_ENABLED": "false",
        "MAX_QUESTIONS_PER_SLOT": "3",
        "REPLY_MODE": "twiml",
        "LOG_LEVEL": "WARNING",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app.agent.graph import reset_graph_cache  # noqa: E402
from app.agent.llm import reset_llm_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import get_engine, init_db, reset_state  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.locks import reset_locks  # noqa: E402
from app.services.whatsapp import SenderRouter, set_sender  # noqa: E402

ADMIN_PHONE_E164 = "+11020645167"
LEAD_PHONE_E164 = "+11020645168"
WEBHOOK_PATH = "/webhooks/twilio/whatsapp"
ADMIN_API_KEY = "test-admin-key"


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
# Twilio helpers
# --------------------------------------------------------------------------- #
def sign_twilio_request(url: str, params: dict[str, str], token: str = "test-auth-token") -> str:
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def inbound_form(
    body: str,
    *,
    from_number: str = "01020645168",
    sid: str | None = None,
    profile_name: str = "John Smith",
) -> dict[str, str]:
    return {
        "From": f"whatsapp:{from_number}",
        "To": "whatsapp:+14155238886",
        "Body": body,
        "MessageSid": sid or f"SM{uuid.uuid4().hex}",
        "ProfileName": profile_name,
        "NumMedia": "0",
        "SmsStatus": "received",
    }


def post_inbound(
    client: TestClient,
    body: str,
    *,
    from_number: str = "01020645168",
    sid: str | None = None,
    profile_name: str = "John Smith",
    signature: str | None = None,
    sign: bool = True,
) -> tuple[object, str]:
    """POST a signed Twilio webhook and return ``(response, reply_text)``."""
    form = inbound_form(body, from_number=from_number, sid=sid, profile_name=profile_name)
    url = f"http://testserver{WEBHOOK_PATH}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if sign:
        headers["X-Twilio-Signature"] = signature or sign_twilio_request(url, form)

    response = client.post(
        WEBHOOK_PATH,
        content=urlencode(form),
        headers=headers,
    )
    return response, extract_twiml_message(response.text)


def extract_twiml_message(xml_text: str) -> str:
    import re

    match = re.search(r"<Message>(.*?)</Message>", xml_text, re.DOTALL)
    if not match:
        return ""
    return (
        match.group(1)
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )

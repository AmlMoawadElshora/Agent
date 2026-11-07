"""Meta WhatsApp Cloud API webhook.

Two endpoints, as defined by Meta:

``GET /webhooks/whatsapp``
    Verification handshake.  Meta calls it with ``hub.mode=subscribe``,
    ``hub.verify_token`` and ``hub.challenge``; we echo the challenge (plain
    text) only when the token matches ``META_VERIFY_TOKEN``.

``POST /webhooks/whatsapp``
    Event delivery (JSON).  Each request may contain message batches *and*
    delivery statuses.  The ``X-Hub-Signature-256`` header (HMAC-SHA256 of the
    raw body with ``META_APP_SECRET``) is verified when a secret is configured.

Replies are sent through the Graph API (see ``app/services/whatsapp.py``); the
webhook itself answers ``200 OK`` with no body, which is what Meta expects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.engine import ConversationEngine
from app.config import Settings, get_settings
from app.db.base import get_db
from app.db.models import Message
from app.domain.phones import PhoneNumberError, mask_phone, normalize_phone
from app.services.whatsapp import get_sender

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Inbound payload types we can turn into conversation text.
_MEDIA_TYPES = {"image", "video", "document", "audio", "sticker"}
_IGNORED_TYPES = {"reaction", "system", "ephemeral", "request_welcome"}


# --------------------------------------------------------------------------- #
# Signature / payload helpers
# --------------------------------------------------------------------------- #
def meta_signature_is_valid(*, app_secret: str, signature_header: str, raw_body: bytes) -> bool:
    """Verify ``X-Hub-Signature-256`` (``sha256=<hex digest>``)."""
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return secrets.compare_digest(expected, signature_header.strip())


def extract_message_content(message: dict[str, Any]) -> tuple[str, int]:
    """Return ``(text, media_count)`` for one Cloud API message object."""
    message_type = message.get("type", "")

    if message_type == "text":
        return str((message.get("text") or {}).get("body") or ""), 0

    if message_type == "button":
        button = message.get("button") or {}
        return str(button.get("text") or button.get("payload") or ""), 0

    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            reply = interactive.get(key)
            if reply:
                return str(reply.get("title") or reply.get("id") or ""), 0
        return "", 0

    if message_type in _MEDIA_TYPES:
        media = message.get(message_type) or {}
        return str(media.get("caption") or ""), 1

    return "", 0


def _iter_events(payload: dict[str, Any]):
    """Yield every ``value`` object in a Cloud API webhook payload."""
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if value:
                yield value


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/whatsapp", response_class=PlainTextResponse)
def verify_webhook(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """Meta verification handshake (``hub.mode`` / ``hub.verify_token``)."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token") or ""
    challenge = params.get("hub.challenge") or ""

    if mode != "subscribe":
        raise HTTPException(status_code=400, detail="unsupported hub.mode")

    expected = settings.meta_verify_token
    if not expected:
        logger.error("META_VERIFY_TOKEN is not configured; rejecting verification")
        raise HTTPException(status_code=403, detail="verify token not configured")

    if not secrets.compare_digest(token, expected):
        logger.warning("webhook verification failed: bad hub.verify_token")
        raise HTTPException(status_code=403, detail="invalid verify token")

    logger.info("webhook verified by meta")
    return PlainTextResponse(challenge)


@router.post("/whatsapp")
async def receive_events(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Handle inbound messages and delivery statuses from Meta."""
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if settings.signature_validation_active:
        if not signature:
            logger.warning("webhook without X-Hub-Signature-256 rejected")
            raise HTTPException(status_code=403, detail="missing X-Hub-Signature-256")
        if not meta_signature_is_valid(
            app_secret=settings.meta_app_secret or "",
            signature_header=signature,
            raw_body=raw_body,
        ):
            logger.warning("invalid meta signature rejected")
            raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        logger.warning("webhook body is not valid JSON")
        raise HTTPException(status_code=400, detail="invalid JSON payload") from None

    if payload.get("object") != "whatsapp_business_account":
        logger.warning("unexpected webhook object: %s", payload.get("object"))
        return Response(status_code=200)

    engine = ConversationEngine(settings=settings, sender=get_sender())
    failures = 0
    handled = 0

    for value in _iter_events(payload):
        handled += _apply_statuses(db, value)

        for message in value.get("messages") or []:
            try:
                if _process_message(db, engine, settings, value, message):
                    handled += 1
            except Exception:  # noqa: BLE001 - keep processing the batch
                db.rollback()
                failures += 1
                logger.exception(
                    "failed to process inbound WhatsApp message",
                    extra={"wamid": message.get("id")},
                )

    logger.info("webhook processed", extra={"messages": handled, "failures": failures})

    if failures:
        # 500 makes Meta retry; wamid dedupe keeps retries idempotent.
        raise HTTPException(status_code=500, detail="failed to process message")

    return Response(status_code=200)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _apply_statuses(db: Session, value: dict[str, Any]) -> int:
    """Persist delivery/read receipts.  Returns the number of updated rows."""
    updated = 0
    for status in value.get("statuses") or []:
        provider_sid = status.get("id")
        state = status.get("status")
        if not provider_sid or not state:
            continue

        message = db.scalar(select(Message).where(Message.provider_sid == provider_sid))
        if message is None:
            logger.debug("status for unknown message ignored", extra={"wamid": provider_sid})
            continue

        message.delivery_status = str(state)
        error = (status.get("errors") or [{}])[0]
        if state == "failed" and error.get("title"):
            details = error.get("error_data") or {}
            message.message_metadata = {
                **(message.message_metadata or {}),
                "delivery_error": {
                    "code": error.get("code"),
                    "title": error.get("title"),
                    "details": details.get("details"),
                },
            }
        updated += 1

    if updated:
        db.commit()
    return updated


def _process_message(
    db: Session,
    engine: ConversationEngine,
    settings: Settings,
    value: dict[str, Any],
    message: dict[str, Any],
) -> bool:
    """Run one inbound Cloud API message through the conversation engine."""
    message_type = message.get("type") or ""
    if message_type in _IGNORED_TYPES:
        logger.debug("ignoring %s message", message_type)
        return False

    wa_from = message.get("from")
    if not wa_from:
        logger.warning("inbound message without a sender ignored")
        return False

    try:
        phone = normalize_phone(wa_from, settings.default_country_code)
    except PhoneNumberError as exc:
        logger.warning("unusable inbound number %s: %s", mask_phone(wa_from), exc)
        return False

    body, num_media = extract_message_content(message)
    contacts = value.get("contacts") or []
    profile = (contacts[0] or {}).get("profile") or {} if contacts else {}
    profile_name = profile.get("name")

    outcome = engine.handle_inbound(
        db,
        from_number=phone,
        body=body,
        provider_sid=message.get("id"),
        profile_name=profile_name,
        num_media=num_media,
        use_graph=True,
    )
    db.commit()

    logger.info(
        "inbound processed",
        extra={
            "lead_id": outcome.lead_id,
            "status": outcome.status,
            "duplicate": outcome.duplicate,
            "qualified": outcome.qualified,
            "handoff": bool(outcome.handoff and outcome.handoff.delivered),
        },
    )
    return True

"""Twilio WhatsApp webhook.

Twilio posts ``application/x-www-form-urlencoded`` payloads.  We validate the
``X-Twilio-Signature`` header, run the conversation engine, and answer with
TwiML so the reply is delivered inside the same HTTP round-trip (no second API
call, no double-send).

Delivery receipts (``SmsStatus`` without ``Body``) are recorded and
acknowledged with an empty 204.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response
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


def twilio_signature_is_valid(
    *, auth_token: str, signature: str, url: str, params: dict[str, str]
) -> bool:
    """Re-implementation of Twilio's request validator (HMAC-SHA1, base64)."""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _public_url(request: Request, settings: Settings) -> str:
    base = settings.public_base_url.rstrip("/")
    if not base:
        base = f"{request.url.scheme}://{request.url.netloc}"
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{request.url.path}{query}"


def _twiml(message: str) -> Response:
    from xml.sax.saxutils import escape

    if not message:
        body = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"
    else:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Message>{escape(message)}</Message></Response>"
        )
    return Response(content=body, media_type="application/xml")


@router.get("/twilio/whatsapp")
def webhook_probe() -> dict[str, str]:
    """Human-friendly check that the URL is reachable (Twilio uses POST)."""
    return {
        "status": "ok",
        "message": "Twilio WhatsApp webhook is live. Configure this URL as the "
        "'WHEN A MESSAGE COMES IN' webhook (HTTP POST).",
    }


@router.post("/twilio/whatsapp")
async def twilio_whatsapp(
    request: Request,
    From: str = Form(...),
    Body: str = Form(""),
    MessageSid: str | None = Form(None),
    ProfileName: str | None = Form(None),
    NumMedia: str = Form("0"),
    SmsStatus: str | None = Form(None),
    To: str | None = Form(None),
    x_twilio_signature: str | None = Header(default=None, alias="X-Twilio-Signature"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    form = await request.form()
    params: dict[str, str] = {str(k): str(v) for k, v in form.items()}

    if settings.signature_validation_active:
        if not x_twilio_signature:
            logger.warning("webhook without signature rejected")
            raise HTTPException(status_code=403, detail="missing X-Twilio-Signature")
        if not twilio_signature_is_valid(
            auth_token=settings.twilio_auth_token or "",
            signature=x_twilio_signature,
            url=_public_url(request, settings),
            params=params,
        ):
            logger.warning("invalid twilio signature rejected")
            raise HTTPException(status_code=403, detail="invalid signature")

    # Delivery receipt (status callback) — no reply needed.
    if SmsStatus and not Body and MessageSid:
        message = db.scalar(select(Message).where(Message.provider_sid == MessageSid))
        if message is not None:
            message.delivery_status = SmsStatus
            db.commit()
        return Response(status_code=204)

    try:
        phone = normalize_phone(From, settings.default_country_code)
    except PhoneNumberError as exc:
        logger.warning("unusable inbound number %s: %s", mask_phone(From), exc)
        return _twiml("")

    try:
        num_media = int(NumMedia or 0)
    except ValueError:
        num_media = 0

    engine = ConversationEngine(settings=settings, sender=get_sender())

    try:
        outcome = engine.handle_inbound(
            db,
            from_number=phone,
            body=Body,
            provider_sid=MessageSid,
            profile_name=ProfileName,
            num_media=num_media,
            use_graph=True,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - always answer Twilio with 200
        db.rollback()
        logger.exception("failed to process inbound WhatsApp message")
        return _twiml(
            "Sorry, we hit a temporary issue on our side. Could you resend that in a moment?"
        )

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

    if outcome.duplicate or outcome.ignored:
        return _twiml("")

    return _twiml(outcome.reply)

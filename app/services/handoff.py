"""Qualification handoff: build the summary card and push it to the admin.

The card is sent to ``ADMIN_WHATSAPP_NUMBER`` (default ``01020645167``) and
every attempt is persisted in the ``handoffs`` table so nothing is silently
lost when Twilio has an outage.  A failed card keeps the lead in
``QUALIFIED`` state, which means the next inbound message retries the handoff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import repository
from app.db.models import Handoff, Lead
from app.domain.enums import HandoffStatus, LeadStatus
from app.domain.schemas import SummaryCard
from app.services.whatsapp import SenderRouter, get_sender

logger = logging.getLogger(__name__)


class NotQualifiedError(ValueError):
    """Raised when a handoff is requested before all slots are collected."""


@dataclass(slots=True)
class HandoffOutcome:
    summary: SummaryCard
    delivered: bool
    error: str | None
    handoff: Handoff
    status: str


def build_summary(lead: Lead, *, notes: str | None = None) -> SummaryCard:
    """Assemble the structured lead card from the persisted lead row."""
    if not lead.is_qualified:
        raise NotQualifiedError(
            "lead is missing price, condition or size — qualification incomplete"
        )

    from app.domain.enums import PropertyCondition

    qualified_at = lead.handoff_at or datetime.now(UTC)
    return SummaryCard(
        lead_id=lead.id,
        phone_number=lead.phone_number,
        full_name=lead.full_name,
        source=lead.source,
        asking_price_usd=int(lead.asking_price_usd or 0),
        property_condition=PropertyCondition(lead.property_condition or "UNKNOWN"),
        condition_raw=lead.condition_raw,
        property_size_sqft=int(lead.property_size_sqft or 0),
        qualified_at=qualified_at,
        turns=lead.turn_count or 0,
        notes=notes,
    )


def perform_handoff(
    db: Session,
    lead: Lead,
    *,
    sender: SenderRouter | None = None,
    settings: Settings | None = None,
    notes: str | None = None,
    force: bool = False,
) -> HandoffOutcome:
    """Send the lead card to the admin exactly once (idempotent)."""
    settings = settings or get_settings()
    sender = sender or get_sender()
    summary = build_summary(lead, notes=notes)
    card_text = summary.to_whatsapp_text()

    # Idempotency: a delivered handoff is never re-sent unless forced.
    existing = repository.latest_handoff(db, lead.id)
    if (
        not force
        and existing is not None
        and existing.status == HandoffStatus.SENT.value
    ):
        logger.info("handoff already delivered", extra={"lead_id": lead.id})
        return HandoffOutcome(
            summary=summary,
            delivered=True,
            error=None,
            handoff=existing,
            status=lead.status,
        )

    handoff = repository.create_handoff(
        db,
        lead,
        admin_phone=settings.admin_phone_e164,
        summary_text=card_text,
        payload=summary.to_dict(),
        status=HandoffStatus.PENDING,
    )

    result = sender.send_whatsapp(settings.admin_phone_e164, card_text)
    now = datetime.now(UTC)

    if result.ok:
        handoff.status = HandoffStatus.SENT.value
        handoff.provider_sid = result.sid
        handoff.sent_at = now
        handoff.error = None
        repository.mark_status(lead, LeadStatus.HANDED_OFF)
        db.flush()
        logger.info(
            "lead handed off to admin",
            extra={
                "lead_id": lead.id,
                "admin": settings.admin_phone_e164,
                "sid": result.sid,
                "dry_run": result.dry_run,
            },
        )
        return HandoffOutcome(
            summary=summary,
            delivered=True,
            error=None,
            handoff=handoff,
            status=lead.status,
        )

    handoff.status = HandoffStatus.FAILED.value
    handoff.error = result.error or "unknown delivery error"
    # Keep the lead QUALIFIED so the next turn retries automatically.
    if LeadStatus(lead.status) not in {LeadStatus.HANDED_OFF, LeadStatus.OPTED_OUT}:
        repository.mark_status(lead, LeadStatus.QUALIFIED)
    db.flush()
    logger.error(
        "handoff delivery failed",
        extra={"lead_id": lead.id, "error": handoff.error},
    )
    return HandoffOutcome(
        summary=summary,
        delivered=False,
        error=handoff.error,
        handoff=handoff,
        status=lead.status,
    )

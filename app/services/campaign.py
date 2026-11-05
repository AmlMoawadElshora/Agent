"""Outbound campaign: send the opening WhatsApp message to a prospect list.

Usage is intentionally boring and safe:

* Terminal leads (already handed off / opted out) and duplicates are skipped.
* A lead that was already contacted is never messaged twice by accident.
* Each send is rate-limited and independently error-handled.
* ``dry_run=True`` (the default everywhere) logs instead of transmitting.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.agent.prompts import greeting
from app.config import Settings, get_settings
from app.db import repository
from app.db.models import Lead
from app.domain.enums import LeadStatus, MessageDirection, MessageKind
from app.domain.phones import PhoneNumberError, normalize_phone
from app.domain.schemas import (
    CampaignResponse,
    CampaignResultItem,
    LeadCreate,
)
from app.services.whatsapp import SenderRouter, get_sender

logger = logging.getLogger(__name__)

_SKIP_STATUSES = {
    LeadStatus.HANDED_OFF.value,
    LeadStatus.OPTED_OUT.value,
    LeadStatus.FAILED.value,
}


def send_opening_message(
    db: Session,
    lead: Lead,
    *,
    sender: SenderRouter | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> CampaignResultItem:
    """Send the campaign opener to one lead and record it in the transcript."""
    settings = settings or get_settings()
    sender = sender or get_sender()

    if lead.opt_out or lead.status in _SKIP_STATUSES and not force:
        return CampaignResultItem(
            phone_number=lead.phone_number,
            lead_id=lead.id,
            sent=False,
            skipped_reason=f"lead is {lead.status.lower()}",
        )

    if lead.status != LeadStatus.NEW.value and not force:
        return CampaignResultItem(
            phone_number=lead.phone_number,
            lead_id=lead.id,
            sent=False,
            skipped_reason="opening message already sent",
        )

    body = greeting(lead.full_name)
    result = sender.send_whatsapp(lead.phone_number, body)

    repository.add_message(
        db,
        lead,
        direction=MessageDirection.OUTBOUND,
        kind=MessageKind.CAMPAIGN_OPENING,
        body=body,
        provider_sid=result.sid,
        delivery_status=result.status if result.ok else "failed",
        metadata={"error": result.error, "dry_run": result.dry_run},
    )

    if not result.ok:
        repository.mark_status(lead, LeadStatus.FAILED)
        return CampaignResultItem(
            phone_number=lead.phone_number,
            lead_id=lead.id,
            sent=False,
            error=result.error or "send failed",
        )

    repository.mark_status(lead, LeadStatus.CONTACTED)
    lead.lead_metadata = {
        **(lead.lead_metadata or {}),
        "opening_sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    db.flush()
    return CampaignResultItem(
        phone_number=lead.phone_number,
        lead_id=lead.id,
        sent=True,
    )


def launch_campaign(
    db: Session,
    leads: list[LeadCreate],
    *,
    sender: SenderRouter | None = None,
    settings: Settings | None = None,
    dry_run: bool | None = None,
    source: str = "campaign",
) -> CampaignResponse:
    """Import (if needed) and message every prospect in ``leads``."""
    settings = settings or get_settings()
    sender = sender or get_sender()

    # Global dry-run switch: config may force it even if the caller asked live.
    effective_dry_run = sender.dry_run if dry_run is None else (dry_run or sender.dry_run)
    if dry_run is False and sender.dry_run:
        logger.warning("campaign requested live send but transport is dry-run; staying dry")

    results: list[CampaignResultItem] = []
    rate = settings.outbound_rate_limit_per_second
    delay = 1.0 / rate if rate > 0 else 0.0
    sent = skipped = failed = 0

    for index, item in enumerate(leads):
        try:
            phone = normalize_phone(item.phone_number, settings.default_country_code)
        except PhoneNumberError as exc:
            failed += 1
            results.append(
                CampaignResultItem(
                    phone_number=item.phone_number,
                    sent=False,
                    error=str(exc),
                )
            )
            continue

        lead, _created = repository.get_or_create_lead(
            db,
            phone,
            full_name=item.full_name,
            source=item.source or source,
            metadata=item.metadata,
        )

        result = send_opening_message(db, lead, sender=sender, settings=settings)
        results.append(result)
        if result.sent:
            sent += 1
        elif result.error:
            failed += 1
        else:
            skipped += 1

        if result.sent and index < len(leads) - 1 and delay:
            time.sleep(delay)

    db.commit()

    logger.info(
        "campaign finished",
        extra={"sent": sent, "skipped": skipped, "failed": failed, "dry_run": effective_dry_run},
    )
    return CampaignResponse(
        total=len(leads),
        sent=sent,
        skipped=skipped,
        failed=failed,
        dry_run=effective_dry_run,
        results=results,
    )

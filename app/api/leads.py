"""Admin REST API: lead management, campaign launch, transcripts, handoffs."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.agent.engine import ConversationEngine
from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.db import repository
from app.db.base import get_db
from app.domain.enums import LeadStatus, MessageDirection, MessageKind
from app.domain.schemas import (
    BulkLeadCreate,
    CampaignRequest,
    CampaignResponse,
    HandoffRead,
    HandoffResponse,
    LeadCreate,
    LeadRead,
    ManualMessageRequest,
    MessageRead,
)
from app.services.campaign import launch_campaign
from app.services.handoff import NotQualifiedError
from app.services.whatsapp import get_sender

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["admin"], dependencies=[Depends(require_admin)])


def _get_lead_or_404(db: Session, lead_id: str):
    lead = repository.get_lead(db, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead


@router.post("/leads", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
def create_lead(payload: LeadCreate, db: Session = Depends(get_db)) -> LeadRead:
    lead, _created = repository.get_or_create_lead(
        db,
        payload.phone_number,
        full_name=payload.full_name,
        source=payload.source,
        metadata=payload.metadata,
    )
    db.commit()
    db.refresh(lead)
    return LeadRead.model_validate(lead)


@router.post("/leads/bulk", response_model=list[LeadRead], status_code=status.HTTP_201_CREATED)
def create_leads_bulk(payload: BulkLeadCreate, db: Session = Depends(get_db)) -> list[LeadRead]:
    leads = []
    for item in payload.leads:
        lead, _created = repository.get_or_create_lead(
            db,
            item.phone_number,
            full_name=item.full_name,
            source=item.source,
            metadata=item.metadata,
        )
        leads.append(lead)
    db.commit()
    for lead in leads:
        db.refresh(lead)
    return [LeadRead.model_validate(lead) for lead in leads]


@router.get("/leads", response_model=list[LeadRead])
def list_leads(
    status_filter: LeadStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[LeadRead]:
    leads = repository.list_leads(db, status=status_filter, limit=limit, offset=offset)
    return [LeadRead.model_validate(lead) for lead in leads]


@router.get("/leads/{lead_id}", response_model=LeadRead)
def get_lead(lead_id: str, db: Session = Depends(get_db)) -> LeadRead:
    return LeadRead.model_validate(_get_lead_or_404(db, lead_id))


@router.get("/leads/{lead_id}/messages", response_model=list[MessageRead])
def get_transcript(
    lead_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[MessageRead]:
    _get_lead_or_404(db, lead_id)
    return [MessageRead.model_validate(m) for m in repository.transcript(db, lead_id, limit=limit)]


@router.post("/leads/{lead_id}/messages", response_model=MessageRead)
def send_manual_message(
    lead_id: str,
    payload: ManualMessageRequest,
    db: Session = Depends(get_db),
) -> MessageRead:
    """Human takeover: send a message from the admin console."""
    lead = _get_lead_or_404(db, lead_id)
    result = get_sender().send_whatsapp(lead.phone_number, payload.body)
    message = repository.add_message(
        db,
        lead,
        direction=MessageDirection.OUTBOUND,
        kind=MessageKind.HUMAN_TAKEOVER,
        body=payload.body,
        provider_sid=result.sid,
        delivery_status=result.status if result.ok else "failed",
        metadata={"error": result.error, "dry_run": result.dry_run, "manual": True},
    )
    db.commit()
    db.refresh(message)
    if not result.ok:
        logger.warning("manual message failed for lead %s: %s", lead_id, result.error)
    return MessageRead.model_validate(message)


@router.post("/leads/{lead_id}/handoff", response_model=HandoffResponse)
def trigger_handoff(
    lead_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HandoffResponse:
    lead = _get_lead_or_404(db, lead_id)
    engine = ConversationEngine(settings=settings, sender=get_sender())
    try:
        outcome = engine.manual_handoff(db, lead)
    except NotQualifiedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return HandoffResponse(
        lead_id=lead.id,
        status=outcome.status,
        summary=outcome.summary,
        delivered=outcome.delivered,
        error=outcome.error,
    )


@router.post("/leads/{lead_id}/opt-out", response_model=LeadRead)
def opt_out_lead(lead_id: str, db: Session = Depends(get_db)) -> LeadRead:
    lead = _get_lead_or_404(db, lead_id)
    repository.mark_opt_out(lead)
    db.commit()
    db.refresh(lead)
    return LeadRead.model_validate(lead)


@router.get("/handoffs", response_model=list[HandoffRead])
def list_handoffs(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[HandoffRead]:
    return [HandoffRead.model_validate(h) for h in repository.list_handoffs(db, limit=limit)]


@router.post("/campaigns/launch", response_model=CampaignResponse)
def launch_campaign_endpoint(
    payload: CampaignRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CampaignResponse:
    """Import a prospect list and send the opening WhatsApp message."""
    return launch_campaign(
        db,
        payload.leads,
        sender=get_sender(),
        settings=settings,
        dry_run=payload.dry_run,
        source=payload.source,
    )

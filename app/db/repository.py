"""Data-access helpers.

Keeping SQL in one module keeps the agent logic free of ORM details and makes
it trivial to unit-test the state machine against an in-memory database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Handoff, Lead, Message, utcnow
from app.domain.enums import (
    HandoffStatus,
    LeadStatus,
    MessageDirection,
    MessageKind,
    PropertyCondition,
)
from app.domain.phones import normalize_phone
from app.domain.schemas import LeadContext


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #
def get_lead(db: Session, lead_id: str) -> Lead | None:
    return db.get(Lead, lead_id)


def get_lead_by_phone(db: Session, phone_number: str) -> Lead | None:
    normalized = normalize_phone(phone_number)
    return db.scalar(select(Lead).where(Lead.phone_number == normalized))


def get_or_create_lead(
    db: Session,
    phone_number: str,
    *,
    full_name: str | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[Lead, bool]:
    """Return ``(lead, created)``.  Existing leads are enriched, never reset."""
    normalized = normalize_phone(phone_number)
    lead = db.scalar(select(Lead).where(Lead.phone_number == normalized))
    if lead is not None:
        if full_name and not lead.full_name:
            lead.full_name = full_name
        if source and not lead.source:
            lead.source = source
        if metadata:
            lead.lead_metadata = {**(lead.lead_metadata or {}), **metadata}
        return lead, False

    lead = Lead(
        phone_number=normalized,
        full_name=full_name,
        source=source,
        status=LeadStatus.NEW.value,
        questions_asked={},
        lead_metadata=metadata or {},
    )
    db.add(lead)
    db.flush()
    return lead, True


def list_leads(
    db: Session, *, status: LeadStatus | None = None, limit: int = 100, offset: int = 0
) -> Sequence[Lead]:
    stmt = select(Lead).order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    if status is not None:
        stmt = stmt.where(Lead.status == status.value)
    return db.scalars(stmt).all()


def count_leads(db: Session, *, status: LeadStatus | None = None) -> int:
    stmt = select(func.count(Lead.id))
    if status is not None:
        stmt = stmt.where(Lead.status == status.value)
    return int(db.scalar(stmt) or 0)


def to_context(lead: Lead) -> LeadContext:
    return LeadContext(
        lead_id=lead.id,
        phone_number=lead.phone_number,
        full_name=lead.full_name,
        status=LeadStatus(lead.status),
        asking_price_usd=lead.asking_price_usd,
        property_condition=lead.condition_enum,
        condition_raw=lead.condition_raw,
        property_size_sqft=lead.property_size_sqft,
        questions_asked=dict(lead.questions_asked or {}),
        turn_count=lead.turn_count or 0,
    )


def apply_context(db: Session, lead: Lead, context: LeadContext) -> Lead:
    """Persist slot values / counters computed by the agent graph."""
    condition: PropertyCondition | None = context.property_condition
    lead.asking_price_usd = context.asking_price_usd
    lead.property_condition = condition.value if condition else None
    lead.condition_raw = context.condition_raw
    lead.property_size_sqft = context.property_size_sqft
    lead.questions_asked = dict(context.questions_asked)
    lead.turn_count = context.turn_count
    lead.status = context.status.value
    db.add(lead)
    return lead


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def add_message(
    db: Session,
    lead: Lead,
    *,
    direction: MessageDirection,
    body: str,
    kind: MessageKind | str = MessageKind.INBOUND,
    provider_sid: str | None = None,
    delivery_status: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Message:
    message = Message(
        lead_id=lead.id,
        direction=direction.value,
        kind=kind.value if hasattr(kind, "value") else str(kind),
        body=body,
        provider_sid=provider_sid,
        delivery_status=delivery_status,
        message_metadata=metadata or {},
    )
    db.add(message)

    now = datetime.now(UTC)
    if direction is MessageDirection.INBOUND:
        lead.last_inbound_at = now
    else:
        lead.last_outbound_at = now
    db.flush()
    return message


def find_message_by_sid(db: Session, provider_sid: str) -> Message | None:
    if not provider_sid:
        return None
    return db.scalar(select(Message).where(Message.provider_sid == provider_sid))


def transcript(db: Session, lead_id: str, *, limit: int = 200) -> Sequence[Message]:
    return db.scalars(
        select(Message)
        .where(Message.lead_id == lead_id)
        .order_by(Message.id.asc())
        .limit(limit)
    ).all()


# --------------------------------------------------------------------------- #
# Handoffs
# --------------------------------------------------------------------------- #
def create_handoff(
    db: Session,
    lead: Lead,
    *,
    admin_phone: str,
    summary_text: str,
    payload: dict[str, Any],
    status: HandoffStatus = HandoffStatus.PENDING,
) -> Handoff:
    handoff = Handoff(
        lead_id=lead.id,
        admin_phone=admin_phone.replace("whatsapp:", ""),
        status=status.value,
        summary_text=summary_text,
        payload=payload,
    )
    db.add(handoff)
    db.flush()
    return handoff


def latest_handoff(db: Session, lead_id: str) -> Handoff | None:
    return db.scalar(
        select(Handoff).where(Handoff.lead_id == lead_id).order_by(Handoff.id.desc()).limit(1)
    )


def list_handoffs(db: Session, *, limit: int = 100) -> Sequence[Handoff]:
    return db.scalars(select(Handoff).order_by(Handoff.id.desc()).limit(limit)).all()


def mark_status(lead: Lead, status: LeadStatus) -> None:
    lead.status = status.value
    if status is LeadStatus.HANDED_OFF:
        lead.handoff_at = utcnow()


def mark_opt_out(lead: Lead) -> None:
    lead.opt_out = True
    lead.status = LeadStatus.OPTED_OUT.value
    lead.lead_metadata = {**(lead.lead_metadata or {}), "opted_out_at": utcnow().isoformat()}

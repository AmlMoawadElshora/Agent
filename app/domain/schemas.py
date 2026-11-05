"""Pydantic schemas used by the agent graph and the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import LeadStatus, PropertyCondition
from app.domain.phones import PhoneNumberError, normalize_phone


# --------------------------------------------------------------------------- #
# Slot extraction
# --------------------------------------------------------------------------- #
class ExtractedSlots(BaseModel):
    """What the extractor believes it heard in a single inbound message.

    Every field is optional: a prospect may answer one, several or none of the
    questions in a single turn.  ``*_evidence`` keeps the raw fragment so the
    summary card / audit trail can show what the lead actually said.
    """

    price_usd: int | None = Field(default=None, ge=1_000, le=100_000_000)
    price_evidence: str | None = None

    condition: PropertyCondition | None = None
    condition_raw: str | None = None

    size_sqft: int | None = Field(default=None, ge=50, le=1_000_000)
    size_evidence: str | None = None

    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: Literal["heuristic", "llm", "merged", "none"] = "none"

    def is_empty(self) -> bool:
        return self.price_usd is None and self.condition is None and self.size_sqft is None


class SummaryCard(BaseModel):
    """Structured card pushed to the acquisitions manager on qualification."""

    lead_id: str
    phone_number: str
    full_name: str | None = None
    source: str | None = None
    asking_price_usd: int
    property_condition: PropertyCondition
    condition_raw: str | None = None
    property_size_sqft: int
    qualified_at: datetime
    turns: int = 0
    notes: str | None = None

    def to_whatsapp_text(self) -> str:
        """Human-readable WhatsApp card (plain text, no markdown tables)."""
        condition = self.property_condition.label
        if self.condition_raw and self.condition_raw.lower() not in condition.lower():
            condition = f'{condition} ("{self.condition_raw}")'
        lines = [
            "🏠 *NEW QUALIFIED LEAD*",
            "────────────────────",
            f"*Name:* {self.full_name or 'Not provided'}",
            f"*Phone:* {self.phone_number}",
            f"*Source:* {self.source or 'WhatsApp outreach'}",
            f"*Asking price:* ${self.asking_price_usd:,.0f}",
            f"*Condition:* {condition}",
            f"*Size:* {self.property_size_sqft:,.0f} sq ft",
            f"*Qualified:* {self.qualified_at.strftime('%Y-%m-%d %H:%M UTC')}",
            f"*Turns:* {self.turns}",
            f"*Lead ID:* {self.lead_id}",
        ]
        if self.notes:
            lines.append(f"*Notes:* {self.notes}")
        lines.append("────────────────────")
        lines.append("Reply to this chat to take over the conversation.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["card_text"] = self.to_whatsapp_text()
        return data



# --------------------------------------------------------------------------- #
# Agent turn
# --------------------------------------------------------------------------- #
class LeadContext(BaseModel):
    """Everything the graph needs to know about the lead for one turn."""

    lead_id: str
    phone_number: str
    full_name: str | None = None
    status: LeadStatus = LeadStatus.IN_PROGRESS
    asking_price_usd: int | None = None
    property_condition: PropertyCondition | None = None
    condition_raw: str | None = None
    property_size_sqft: int | None = None
    questions_asked: dict[str, int] = Field(default_factory=dict)
    turn_count: int = 0

    def collected(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.asking_price_usd is not None:
            data["price"] = self.asking_price_usd
        if self.property_condition is not None:
            data["condition"] = self.property_condition
        if self.property_size_sqft is not None:
            data["size"] = self.property_size_sqft
        return data


class TurnResult(BaseModel):
    """Outcome of processing one inbound message."""

    reply: str = ""
    lead: LeadContext
    extracted: ExtractedSlots = Field(default_factory=ExtractedSlots)
    next_slot: str | None = None
    qualified: bool = False
    handoff_required: bool = False
    opted_out: bool = False
    human_requested: bool = False
    guardrail: str | None = None
    message_kind: str = "FOLLOW_UP"


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
class LeadCreate(BaseModel):
    phone_number: str
    full_name: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] | None = None

    @field_validator("phone_number")
    @classmethod
    def _validate_phone(cls, value: str) -> str:
        try:
            return normalize_phone(value)
        except PhoneNumberError as exc:
            raise ValueError(str(exc)) from exc


class BulkLeadCreate(BaseModel):
    leads: list[LeadCreate] = Field(min_length=1, max_length=1000)


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    phone_number: str
    full_name: str | None
    source: str | None
    status: LeadStatus
    asking_price_usd: int | None
    property_condition: PropertyCondition | None
    condition_raw: str | None
    property_size_sqft: int | None
    questions_asked: dict[str, int]
    turn_count: int
    opt_out: bool
    last_question_slot: str | None
    created_at: datetime
    updated_at: datetime
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    handoff_at: datetime | None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: str
    direction: str
    kind: str
    body: str
    provider_sid: str | None
    delivery_status: str | None
    created_at: datetime


class HandoffRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: str
    admin_phone: str
    status: str
    provider_sid: str | None
    error: str | None
    created_at: datetime
    sent_at: datetime | None


class CampaignRequest(BaseModel):
    leads: list[LeadCreate] = Field(min_length=1, max_length=5000)
    dry_run: bool = True
    source: str = "campaign"


class CampaignResultItem(BaseModel):
    phone_number: str
    lead_id: str | None = None
    sent: bool = False
    skipped_reason: str | None = None
    error: str | None = None


class CampaignResponse(BaseModel):
    total: int
    sent: int
    skipped: int
    failed: int
    dry_run: bool
    results: list[CampaignResultItem]


class ManualMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class HandoffResponse(BaseModel):
    lead_id: str
    status: str
    summary: SummaryCard
    delivered: bool
    error: str | None = None

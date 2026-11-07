"""ORM models: leads, conversation messages and handoff audit trail."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.enums import (
    HandoffStatus,
    LeadStatus,
    MessageDirection,
    MessageKind,
    PropertyCondition,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base shared by every model."""


class Lead(Base):
    """A prospect and the qualification state of their conversation."""

    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(100))

    status: Mapped[str] = mapped_column(
        String(20), default=LeadStatus.NEW.value, index=True, nullable=False
    )

    # --- collected qualification slots -----------------------------------
    asking_price_usd: Mapped[int | None] = mapped_column(Integer)
    property_condition: Mapped[str | None] = mapped_column(String(20))
    condition_raw: Mapped[str | None] = mapped_column(String(250))
    property_size_sqft: Mapped[int | None] = mapped_column(Integer)

    # --- conversation bookkeeping ----------------------------------------
    questions_asked: Mapped[dict[str, int]] = mapped_column(JSON, default=dict, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_question_slot: Mapped[str | None] = mapped_column(String(20))
    opt_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lead_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list[Message]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="Message.id"
    )
    handoffs: Mapped[list[Handoff]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="Handoff.id"
    )

    # --- convenience ------------------------------------------------------
    @property
    def condition_enum(self) -> PropertyCondition | None:
        return PropertyCondition(self.property_condition) if self.property_condition else None

    @property
    def is_qualified(self) -> bool:
        return (
            self.asking_price_usd is not None
            and self.property_condition is not None
            and self.property_size_sqft is not None
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Lead {self.phone_number} status={self.status}>"



class Message(Base):
    """One inbound or outbound WhatsApp message (full transcript)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), default=MessageKind.INBOUND.value, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Meta message id (wamid...) — unique so webhook retries never double-process.
    provider_sid: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    delivery_status: Mapped[str | None] = mapped_column(String(30))
    message_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    lead: Mapped[Lead] = relationship(back_populates="messages")

    @property
    def is_inbound(self) -> bool:
        return self.direction == MessageDirection.INBOUND.value

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message {self.direction} {self.kind}>"


class Handoff(Base):
    """Audit trail of lead summary cards pushed to the admin."""

    __tablename__ = "handoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    admin_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=HandoffStatus.PENDING.value, index=True, nullable=False
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lead: Mapped[Lead] = relationship(back_populates="handoffs")


# Hot path: fetch the transcript of one lead in order.
Index("ix_messages_lead_created", Message.lead_id, Message.created_at)

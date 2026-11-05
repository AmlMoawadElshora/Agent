"""Conversation engine: glues the graph to the database and WhatsApp.

One method matters: :meth:`ConversationEngine.handle_inbound`.  It is the whole
runtime contract of the bot:

    lock -> dedupe -> persist inbound -> run graph -> persist slots
         -> handoff (if qualified) -> deliver reply

Everything is wrapped in the caller's transaction; the webhook commits once, so
a crash mid-turn never leaves a half-updated lead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.agent import graph as conversation_graph
from app.agent import prompts
from app.agent.graph import ConversationState
from app.config import Settings, get_settings
from app.db import repository
from app.db.models import Lead
from app.domain.enums import LeadStatus, MessageDirection, MessageKind, PropertyCondition
from app.domain.phones import PhoneNumberError, normalize_phone
from app.domain.schemas import CampaignResultItem, ExtractedSlots, LeadContext
from app.services.handoff import HandoffOutcome, perform_handoff
from app.services.locks import lead_lock
from app.services.whatsapp import SenderRouter, get_sender

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InboundOutcome:
    reply: str = ""
    lead_id: str = ""
    status: str = LeadStatus.IN_PROGRESS.value
    duplicate: bool = False
    ignored: bool = False
    qualified: bool = False
    handoff: HandoffOutcome | None = None
    extracted: ExtractedSlots | None = None
    error: str | None = None


class ConversationEngine:
    """Stateless service; safe to instantiate per request."""

    def __init__(
        self,
        *,
        sender: SenderRouter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sender = sender or get_sender()

    # ------------------------------------------------------------------ #
    # Inbound
    # ------------------------------------------------------------------ #
    def handle_inbound(
        self,
        db: Session,
        *,
        from_number: str,
        body: str | None,
        provider_sid: str | None = None,
        profile_name: str | None = None,
        num_media: int = 0,
        use_graph: bool = True,
    ) -> InboundOutcome:
        text = (body or "").strip()

        try:
            target = normalize_phone(from_number, self.settings.default_country_code)
        except PhoneNumberError as exc:
            logger.warning("ignoring inbound message with invalid number: %s", exc)
            return InboundOutcome(ignored=True, error=str(exc))

        with lead_lock(target) as locked:
            if not locked:
                logger.warning("proceeding without lead lock", extra={"phone": target})

            lead, _created = repository.get_or_create_lead(
                db,
                target,
                full_name=profile_name,
                source="whatsapp_inbound",
            )

            # Twilio retries webhooks for up to 15 minutes; MessageSid dedupe
            # guarantees a reply is never generated twice.
            if provider_sid and repository.find_message_by_sid(db, provider_sid):
                logger.info("duplicate webhook ignored", extra={"sid": provider_sid})
                return InboundOutcome(
                    lead_id=lead.id,
                    status=lead.status,
                    duplicate=True,
                    ignored=True,
                )

            repository.add_message(
                db,
                lead,
                direction=MessageDirection.INBOUND,
                kind=MessageKind.INBOUND,
                body=text or "[media only]",
                provider_sid=provider_sid,
                metadata={"num_media": num_media, "profile_name": profile_name},
            )

            if lead.opt_out or lead.status == LeadStatus.OPTED_OUT.value:
                logger.info("ignoring message from opted-out lead", extra={"lead_id": lead.id})
                return InboundOutcome(lead_id=lead.id, status=lead.status, ignored=True)

            if not text:
                reply = prompts.MEDIA_REPLY
                self._deliver(db, lead, reply, MessageKind.FOLLOW_UP)
                return InboundOutcome(reply=reply, lead_id=lead.id, status=lead.status)

            state = self._build_state(db, lead, text)
            result = self._run(state, use_graph=use_graph)
            extracted = self._extracted_from(result)

            self._persist_result(db, lead, result)

            if result.get("opt_out"):
                repository.mark_opt_out(lead)
                reply, kind = result.get("reply") or prompts.OPT_OUT_REPLY, MessageKind.OPT_OUT
                delivered = self._deliver(db, lead, reply, kind)
                return InboundOutcome(
                    reply=delivered,
                    lead_id=lead.id,
                    status=lead.status,
                    extracted=extracted,
                )

            qualified = bool(result.get("qualified"))
            handoff: HandoffOutcome | None = None

            if qualified and lead.status != LeadStatus.HANDED_OFF.value:
                # A handoff that previously failed is retried on this turn.
                repository.mark_status(lead, LeadStatus.QUALIFIED)
                handoff = self._handoff(db, lead)
            elif LeadStatus(lead.status) in {LeadStatus.NEW, LeadStatus.CONTACTED}:
                repository.mark_status(lead, LeadStatus.IN_PROGRESS)

            if result.get("human_requested"):
                lead.lead_metadata = {
                    **(lead.lead_metadata or {}),
                    "human_requested_at": repository.utcnow().isoformat(),
                }

            reply_text = result.get("reply") or ""
            kind = result.get("message_kind") or MessageKind.FOLLOW_UP.value
            if (
                kind == MessageKind.QUALIFICATION_COMPLETE.value
                and not self.settings.handoff_notify_lead
            ):
                # Config says: notify only the admin, not the prospect.
                reply_text = ""
            delivered = self._deliver(db, lead, reply_text, kind) if reply_text else ""

            db.flush()
            return InboundOutcome(
                reply=delivered,
                lead_id=lead.id,
                status=lead.status,
                qualified=qualified,
                handoff=handoff,
                extracted=extracted,
            )

    # ------------------------------------------------------------------ #
    # Outbound
    # ------------------------------------------------------------------ #
    def send_opening(self, db: Session, lead: Lead, *, force: bool = False) -> CampaignResultItem:
        """Send the campaign opener to a single lead."""
        from app.services.campaign import send_opening_message

        return send_opening_message(
            db, lead, sender=self.sender, settings=self.settings, force=force
        )

    def manual_handoff(
        self, db: Session, lead: Lead, *, notes: str | None = None
    ) -> HandoffOutcome:
        """Force a handoff (admin action / retry)."""
        return perform_handoff(
            db,
            lead,
            sender=self.sender,
            settings=self.settings,
            notes=notes,
            force=True,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _build_state(self, db: Session, lead: Lead, text: str) -> ConversationState:
        context = repository.to_context(lead)
        history = [
            {
                "role": (
                    "assistant"
                    if message.direction == MessageDirection.OUTBOUND.value
                    else "user"
                ),
                "content": message.body,
            }
            for message in repository.transcript(db, lead.id, limit=12)
        ]
        return ConversationState(
            lead_id=lead.id,
            phone_number=lead.phone_number,
            full_name=lead.full_name,
            status=lead.status,
            slot_hint=lead.last_question_slot,
            already_handed_off=lead.status == LeadStatus.HANDED_OFF.value,
            condition_raw=lead.condition_raw,
            message=text,
            history=history,
            collected=context.collected(),
            questions_asked=dict(lead.questions_asked or {}),
            turn_count=(lead.turn_count or 0) + 1,
        )

    def _run(self, state: ConversationState, *, use_graph: bool) -> ConversationState:
        if use_graph:
            try:
                return conversation_graph.run_turn(state)
            except Exception as exc:  # noqa: BLE001
                logger.exception("graph execution failed, using fallback path: %s", exc)
        try:
            return conversation_graph.run_turn_deterministic(state)
        except Exception as exc:  # noqa: BLE001 - last-resort safety net
            logger.exception("conversation turn failed entirely: %s", exc)
            fallback = dict(state)  # type: ignore[assignment]
            fallback.update(
                {
                    "reply": prompts.FALLBACK_REPLY,
                    "message_kind": MessageKind.FOLLOW_UP.value,
                    "handoff_required": False,
                }
            )
            return fallback


    @staticmethod
    def _extracted_from(result: ConversationState) -> ExtractedSlots:
        try:
            return ExtractedSlots.model_validate(result.get("extracted") or {})
        except Exception:  # noqa: BLE001 - never fail a turn on analytics
            return ExtractedSlots()

    def _persist_result(self, db: Session, lead: Lead, result: ConversationState) -> None:
        collected = result.get("collected") or {}
        context: LeadContext = repository.to_context(lead)

        if "price" in collected:
            context.asking_price_usd = int(collected["price"])
        if "condition" in collected:
            try:
                context.property_condition = PropertyCondition(str(collected["condition"]))
            except ValueError:
                context.property_condition = None
        if "size" in collected:
            context.property_size_sqft = int(collected["size"])

        context.condition_raw = result.get("condition_raw") or lead.condition_raw
        context.questions_asked = dict(
            result.get("questions_asked") or lead.questions_asked or {}
        )
        context.turn_count = (lead.turn_count or 0) + 1
        repository.apply_context(db, lead, context)

        if result.get("next_slot"):
            lead.last_question_slot = result["next_slot"]

        extracted = self._extracted_from(result)
        if not extracted.is_empty():
            lead.lead_metadata = {
                **(lead.lead_metadata or {}),
                "last_extraction": extracted.model_dump(mode="json"),
            }

    def _deliver(self, db: Session, lead: Lead, text: str, kind: Any) -> str:
        """Persist and transmit an outbound message.

        Returns the text that must be returned as TwiML (empty when the message
        was already sent through the REST API).
        """
        if not text:
            return ""

        kind_value = kind.value if hasattr(kind, "value") else str(kind)

        if self.settings.reply_mode == "api":
            result = self.sender.send_whatsapp(lead.phone_number, text)
            repository.add_message(
                db,
                lead,
                direction=MessageDirection.OUTBOUND,
                kind=kind_value,
                body=text,
                provider_sid=result.sid,
                delivery_status=result.status if result.ok else "failed",
                metadata={"error": result.error, "dry_run": result.dry_run},
            )
            if result.ok:
                return ""
            logger.warning("REST reply failed; falling back to TwiML delivery")

        repository.add_message(
            db,
            lead,
            direction=MessageDirection.OUTBOUND,
            kind=kind_value,
            body=text,
            delivery_status="queued_twiml",
        )
        return text

    def _handoff(self, db: Session, lead: Lead) -> HandoffOutcome | None:
        try:
            return perform_handoff(db, lead, sender=self.sender, settings=self.settings)
        except Exception as exc:  # noqa: BLE001 - never lose the prospect's reply
            logger.exception("handoff failed: %s", exc)
            return None

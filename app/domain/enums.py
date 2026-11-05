"""Domain enumerations shared by the database, agent and API layers."""

from __future__ import annotations

from enum import StrEnum


class LeadStatus(StrEnum):
    """Lifecycle of a prospect.

    NEW           imported / first contact not sent yet
    CONTACTED     opening message delivered, no reply yet
    IN_PROGRESS   two-way conversation, qualifying
    QUALIFIED     all three slots collected, handoff in flight
    HANDED_OFF    summary card delivered to the admin
    OPTED_OUT     prospect asked to stop
    FAILED        delivery permanently failed
    """

    NEW = "NEW"
    CONTACTED = "CONTACTED"
    IN_PROGRESS = "IN_PROGRESS"
    QUALIFIED = "QUALIFIED"
    HANDED_OFF = "HANDED_OFF"
    OPTED_OUT = "OPTED_OUT"
    FAILED = "FAILED"

    @classmethod
    def terminal(cls) -> set[LeadStatus]:
        return {cls.HANDED_OFF, cls.OPTED_OUT, cls.FAILED}


class Slot(StrEnum):
    """The three mandatory qualification slots."""

    PRICE = "price"
    CONDITION = "condition"
    SIZE = "size"


class PropertyCondition(StrEnum):
    TURNKEY = "TURNKEY"
    MINOR_REPAIRS = "MINOR_REPAIRS"
    FULL_REHAB = "FULL_REHAB"
    UNKNOWN = "UNKNOWN"

    @property
    def label(self) -> str:
        return {
            self.TURNKEY: "Turnkey / move-in ready",
            self.MINOR_REPAIRS: "Minor cosmetic repairs",
            self.FULL_REHAB: "Full rehab required",
            self.UNKNOWN: "Not specified",
        }[self]


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageKind(StrEnum):
    """Why a message exists — useful for analytics and replay."""

    CAMPAIGN_OPENING = "CAMPAIGN_OPENING"
    QUALIFICATION_QUESTION = "QUALIFICATION_QUESTION"
    FOLLOW_UP = "FOLLOW_UP"
    ANSWER_ACK = "ANSWER_ACK"
    GUARDRAIL = "GUARDRAIL"
    QUALIFICATION_COMPLETE = "QUALIFICATION_COMPLETE"
    OPT_OUT = "OPT_OUT"
    SUMMARY_CARD = "SUMMARY_CARD"
    HUMAN_TAKEOVER = "HUMAN_TAKEOVER"
    INBOUND = "INBOUND"


class HandoffStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"

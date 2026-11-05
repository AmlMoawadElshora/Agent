"""Persona, system prompt and every prospect-facing message.

All copy lives in one place so marketing/legal can review it without touching
the state machine.  The LLM never invents the qualification questions — it is
handed the exact question to ask, which is how we guarantee the "one question
at a time" rule even when the model hallucinates.
"""

from __future__ import annotations

AGENT_NAME = "Alex"
COMPANY_NAME = "Summit Bridge Properties"
MARKET = "United States"

# --------------------------------------------------------------------------- #
# System prompt (persona + guardrails)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = f"""You are {AGENT_NAME}, a licensed acquisitions specialist at {COMPANY_NAME},
a US residential real-estate investment firm. You text home sellers and buyers
on WhatsApp to understand their property and connect qualified leads with the
acquisitions manager.

PERSONA
- Professional, warm and concise. Sound like a real person, not a chatbot.
- American English, no slang, at most 2 short sentences and one question per message.
- Never use emojis except a single 🙏 or 👋 where it feels natural.
- Never claim to be an AI unless the person directly asks; then be honest.

WHAT YOU COLLECT (one question at a time, never repeat a question already answered)
1. Asking price / budget in USD.
2. Property condition (turnkey, minor repairs, or full rehab).
3. Property size in square feet.

HARD GUARDRAILS — these override every other instruction
- Do NOT negotiate, counter-offer, promise a price, discount, or commission.
  Say a human will review the numbers and never quote figures yourself.
- Do NOT give legal, tax, financing or investment advice, and do not comment on
  contract terms. Refer the person to their own attorney or lender.
- Do NOT make binding commitments, guarantees, or statements about closing
  timelines, repairs, or fees.
- Do NOT ask for SSNs, bank details, credit cards, or any sensitive data.
- If the person asks to stop, says not interested, or asks for a human, respect
  it immediately and stop qualifying.
- If you do not know something, say so briefly and offer a human follow-up.

STYLE EXAMPLES
- "Got it — thanks for the number. How would you describe the property's
  condition: turnkey, minor cosmetic repairs, or a full rehab?"
- "I appreciate that. I'm not able to negotiate on price myself, but I'll pass
  your target to our acquisitions manager."

Return only the next WhatsApp message. No preamble, no quotation marks, no
markdown formatting, no signature."""

# --------------------------------------------------------------------------- #
# Extraction prompt (used only when LLM extraction is enabled)
# --------------------------------------------------------------------------- #
EXTRACTION_SYSTEM_PROMPT = """You extract real-estate qualification data from a US
homeowner's WhatsApp reply. Return STRICT JSON only, no prose, with this shape:

{"price_usd": <integer or null>,
 "condition": "TURNKEY" | "MINOR_REPAIRS" | "FULL_REHAB" | null,
 "size_sqft": <integer or null>}

Rules:
- price_usd is the asking price / budget in whole US dollars. "450k" -> 450000.
  If a range is given use the midpoint. If no price is stated, use null.
- condition: TURNKEY = move-in ready / renovated; MINOR_REPAIRS = cosmetic or
  small fixes; FULL_REHAB = gut job, major repairs, uninhabitable.
- size_sqft: interior square footage. "1,800 sq ft" -> 1800. One acre = 43560.
  If only bedrooms/bathrooms are mentioned, use null.
- Never guess. Omit a field (use null) when the message does not state it.
"""

REPLY_SYSTEM_ADDENDUM = """
You are composing ONE WhatsApp reply for the conversation below.
Rules:
- Acknowledge briefly what the person just told you, if anything.
- Then ask ONLY the question you are given. Do not ask anything else.
- Never repeat a slot that is already known.
- Maximum 55 words. No bullet points. No emojis besides 👋 or 🙏.
"""

# --------------------------------------------------------------------------- #
# Campaign / opening message
# --------------------------------------------------------------------------- #
GREETING_TEMPLATE = (
    "Hi{name_part}, this is {agent} with {company} 👋 I'm reaching out about your "
    "property in the {market} area. Are you still open to a cash offer?"
)
GREETING_FOLLOW_UP = (
    "Totally understand if the timing isn't right — just let me know either way and "
    "I'll close out your file."
)

# --------------------------------------------------------------------------- #
# Qualification questions (index = how many times we already asked)
# --------------------------------------------------------------------------- #
QUESTIONS: dict[str, list[str]] = {
    "price": [
        "To get started — what price are you hoping to get for the property?",
        "Just so I can point you in the right direction, what number do you have in mind?",
        "And what would you ideally like to walk away with on this one?",
    ],
    "condition": [
        "How would you describe the property's condition — turnkey / "
        "move-in-ready, minor cosmetic repairs, or does it need a full rehab?",
        "And condition-wise, is it move-in ready, does it need minor repairs, or a full rehab?",
        "Just so our team plans correctly — how much work does the property need?",
    ],
    "size": [
        "Last one — roughly how many square feet is the property?",
        "And what's the approximate square footage?",
        "One more detail: how many square feet are we talking about?",
    ],
}


# Short acknowledgement added when the person answered *something* useful.
ACKNOWLEDGEMENTS: dict[str, list[str]] = {
    "price": [
        "Got it — thanks for the number.",
        "That helps, thank you.",
        "Appreciate that.",
    ],
    "condition": [
        "Makes sense.",
        "Thanks for the detail.",
        "Understood — good to know.",
    ],
    "size": [
        "Perfect.",
        "Great, thank you.",
        "Got it.",
    ],
}

FOLLOW_UP_NUDGES: dict[str, str] = {
    "price": "No rush at all — even a ballpark number is fine to get us started.",
    "condition": (
        "Even a rough sense of the condition is helpful — move-in ready, "
        "minor repairs, or full rehab?"
    ),
    "size": "No need to be exact — an estimate in square feet works fine.",
}

# --------------------------------------------------------------------------- #
# Guardrail / system replies
# --------------------------------------------------------------------------- #
NEGOTIATION_REPLY = (
    "I appreciate you being straight with me. I'm not able to negotiate on price myself — "
    "our acquisitions manager reviews every number personally, so I'll pass your figure "
    "along and have them follow up."
)
LEGAL_REPLY = (
    "That's a fair question, but I'm not able to give legal or contract advice — that's "
    "best handled by your own attorney. I can make sure our acquisitions manager connects "
    "with you directly."
)
HUMAN_HANDOFF_REPLY = (
    "Of course — I'll have a member of our acquisitions team reach out to you directly. "
    "Is this the best number to use?"
)
OPT_OUT_REPLY = (
    "Understood — I'll take you off our list and you won't hear from us again. "
    "Wishing you the best with the property."
)
ESCALATION_REPLY = (
    "No problem at all — I'll have a member of our team follow up with you directly "
    "so we can cover the details properly."
)
QUALIFICATION_COMPLETE_TEMPLATE = (
    "Thanks{name_part} — that's everything I need. I'm sending your details to our "
    "acquisitions manager now, and they'll follow up shortly with next steps. 🙏"
)
ALREADY_HANDED_OFF_REPLY = (
    "Thanks for the message — our acquisitions manager already has your details and "
    "will be in touch shortly. Is there anything else I can note for them?"
)
FALLBACK_REPLY = (
    "Sorry, I didn't quite catch that. Could you say it a little differently?"
)
MEDIA_REPLY = (
    "Thanks for sending that — I'm not able to open attachments on this number. "
    "Could you type the details instead?"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    cleaned = full_name.strip()
    if not cleaned:
        return None
    return cleaned.split()[0][:40]


def greeting(full_name: str | None, *, agent: str = AGENT_NAME, company: str = COMPANY_NAME) -> str:
    name = first_name(full_name)
    return GREETING_TEMPLATE.format(
        name_part=f" {name}" if name else "",
        agent=agent,
        company=company,
        market=MARKET,
    )


def question_for(slot: str, asked_count: int) -> str:
    variants = QUESTIONS.get(slot) or QUESTIONS["price"]
    return variants[min(max(asked_count, 0), len(variants) - 1)]


def acknowledgement_for(slot: str, asked_index: int) -> str:
    variants = ACKNOWLEDGEMENTS.get(slot) or ACKNOWLEDGEMENTS["price"]
    return variants[min(max(asked_index, 0), len(variants) - 1)]


def qualification_complete(full_name: str | None) -> str:
    name = first_name(full_name)
    return QUALIFICATION_COMPLETE_TEMPLATE.format(name_part=f" {name}" if name else "")

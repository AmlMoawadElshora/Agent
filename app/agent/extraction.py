"""Slot extraction: turns free-form WhatsApp replies into structured data.

The extractor is intentionally deterministic first and LLM-assisted second:

* ``extract_slots`` (regex + keyword rules) always runs and is what the tests
  exercise.  It handles the messy reality of WhatsApp — ``$450k``,
  ``around 400-450 thousand``, ``needs a full gut rehab``, ``2,000 sqft``,
  ``half an acre`` — and never invents a value.
* :class:`app.agent.llm.SlotExtractionLLM` can be layered on top for unusual
  phrasing.  Its answer is validated by the same Pydantic schema and merged
  only into slots the rules left empty.

Confidence is reported so the engine can decide whether a value is safe to
accept (it always is here, but it powers analytics and future review queues).
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.enums import PropertyCondition
from app.domain.schemas import ExtractedSlots

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
PRICE_MIN = 1_000
PRICE_MAX = 100_000_000
SIZE_MIN = 50
SIZE_MAX = 1_000_000

_SCALES: dict[str, int] = {
    "k": 1_000,
    "thousand": 1_000,
    "grand": 1_000,
    "m": 1_000_000,
    "mm": 1_000_000,
    "million": 1_000_000,
    "mil": 1_000_000,
}

_FULL_REHAB_TERMS = (
    "full rehab",
    "full gut",
    "gut rehab",
    "complete rehab",
    "complete renovation",
    "full renovation",
    "needs everything",
    "needs a lot of work",
    "needs lots of work",
    "major repairs",
    "major work",
    "fixer upper",
    "fixer-upper",
    "tear down",
    "teardown",
    "distressed",
    "uninhabitable",
    "gutted",
    "shell",
    "needs full",
    "heavy rehab",
    "total rehab",
    "down to the studs",
)

_MINOR_REPAIRS_TERMS = (
    "minor repair",
    "minor repairs",
    "minor work",
    "cosmetic",
    "small fixes",
    "little work",
    "a little work",
    "some work",
    "light rehab",
    "needs paint",
    "needs painting",
    "paint and carpet",
    "some paint",
    "needs some paint",
    "needs carpet",
    "needs flooring",
    "a few repairs",
    "small rehab",
    "dated",
    "needs updating",
    "needs a refresh",
)

_TURNKEY_TERMS = (
    "turnkey",
    "turn key",
    "turn-key",
    "move in ready",
    "move-in ready",
    "movein ready",
    "ready to move",
    "ready to move in",
    "fully renovated",
    "recently renovated",
    "newly renovated",
    "new construction",
    "brand new",
    "excellent condition",
    "great condition",
    "good condition",
    "perfect condition",
    "no work needed",
    "needs no work",
    "doesn't need any work",
    "updated",
    "remodeled",
    "renovated",
)

_OPT_OUT_TERMS = (
    "stop",
    "stop messaging",
    "unsubscribe",
    "remove me",
    "remove my number",
    "do not message",
    "don't message",
    "dont message",
    "no more messages",
    "opt out",
    "opt-out",
    "not interested",
    "leave me alone",
)

_HUMAN_TERMS = (
    "human",
    "real person",
    "a person",
    "speak to someone",
    "talk to someone",
    "speak with someone",
    "talk to a manager",
    "call me",
    "phone me",
    "give me a call",
    "agent",
    "representative",
    "supervisor",
)

_NEGOTIATION_TERMS = (
    "negotiate",
    "negotiation",
    "lower the price",
    "lower your price",
    "come down",
    "discount",
    "best price",
    "best offer",
    "can you do better",
    "too low",
    "counter offer",
    "counter-offer",
    "haggle",
    "sweeten",
    "reduce",
    "cheaper",
)

_LEGAL_TERMS = (
    "legal advice",
    "attorney",
    "lawyer",
    "lawsuit",
    "sue you",
    "contract clause",
    "legally binding",
    "is it legal",
    "terms and conditions",
    "liability",
    "escrow law",
)

# --------------------------------------------------------------------------- #
# Regexes
# --------------------------------------------------------------------------- #
_CURRENCY_RE = re.compile(
    r"\$\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>k|m|mm|million|mil|thousand|grand)?\b",
    re.IGNORECASE,
)
_SCALED_RE = re.compile(
    r"(?<![\d.])(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>k|m|mm|million|mil|thousand|grand)\b",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    r"(?P<cur1>\$)?\s*(?P<num1>\d[\d,]*(?:\.\d+)?)\s*(?P<unit1>k|m|mm|million|mil|thousand|grand)?"
    r"\s*(?:-|–|—|to|and)\s*(?P<cur2>\$)?\s*(?P<num2>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit2>k|m|mm|million|mil|thousand|grand)\b",
    re.IGNORECASE,
)
_CEILING_RE = re.compile(
    r"(?:up to|under|below|max(?:imum)?|no more than|around|about|approximately|close to|"
    r"roughly|in the|looking for|asking|want|need)\s*\$?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>k|m|mm|million|mil|thousand|grand)?",
    re.IGNORECASE,
)
_BARE_NUMBER_RE = re.compile(r"(?<![\d.])(?P<num>\d[\d,]*(?:\.\d+)?)(?![\d.])")

_SQFT_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>k)?\s*"
    r"(?:sq\.?\s*ft\.?|sqft|sq\s*feet|square\s*feets?|square\s*foot|square\s*ft\.?|sf)\b",
    re.IGNORECASE,
)
_SQFT_HYPHEN_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>k)?\s*-?\s*(?:square|sq)\s*-?\s*(?:foot|feet|ft)\b",
    re.IGNORECASE,
)
_ACRES_RE = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?:acres?|acre)\b", re.IGNORECASE)

_PRICE_HINT_WORDS = (
    "price",
    "asking",
    "offer",
    "budget",
    "sell for",
    "selling",
    "want",
    "looking for",
    "expect",
    "value",
    "worth",
)
_SIZE_HINT_WORDS = ("sq", "square", "feet", "foot", "ft", "size", "sf")



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(text: str) -> str:
    """Normalise the message for matching while keeping offsets intact."""
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def _amount(num: str, unit: str | None) -> int | None:
    try:
        value = float(num.replace(",", "").rstrip("."))
    except (TypeError, ValueError):
        return None
    if unit:
        value *= _SCALES.get(unit.lower(), 1)
    return int(round(value))


def _plausible_price(value: int | None) -> bool:
    return value is not None and PRICE_MIN <= value <= PRICE_MAX


def _mask(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _price_working_text(text: str) -> str:
    """Remove size / bedroom / bath numbers so they are never read as money."""
    working = _mask(text, _ACRES_RE)
    working = _mask(working, _SQFT_RE)
    working = _mask(working, _SQFT_HYPHEN_RE)
    return re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:bed|beds|bedroom|bedrooms|bath|baths|bathroom|bathrooms)\b",
        lambda m: " " * len(m.group(0)),
        working,
        flags=re.IGNORECASE,
    )


# --------------------------------------------------------------------------- #
# Price
# --------------------------------------------------------------------------- #
def extract_price(text: str, *, slot_hint: str | None = None) -> tuple[int | None, str | None]:
    """Return ``(price_usd, evidence)``.

    Understands ``$450,000``, ``450k``, ``1.2m``, ``400-450k`` (midpoint),
    ``up to 500k`` (ceiling) and a bare ``450`` when we explicitly asked for a
    price.  Sizes (``2,000 sq ft``) and bed/bath counts are masked out first.
    """
    working = _clean(_price_working_text(text))
    lowered = working.lower()

    # 1) Ranges -> midpoint ("between 400k and 450k")
    for match in _RANGE_RE.finditer(working):
        scale = match.group("unit2") or match.group("unit1")
        low = _amount(match.group("num1"), match.group("unit1") or scale)
        high = _amount(match.group("num2"), match.group("unit2") or scale)
        if not (_plausible_price(low) and _plausible_price(high)):
            continue
        if low > high:
            low, high = high, low
        return (low + high) // 2, match.group(0).strip()

    # 2) Explicit currency
    for match in _CURRENCY_RE.finditer(working):
        value = _amount(match.group("num"), match.group("unit"))
        if _plausible_price(value):
            return value, match.group(0).strip()

    # 3) Scaled numbers ("450k", "1.2 million", "around 500 thousand")
    for match in _SCALED_RE.finditer(working):
        value = _amount(match.group("num"), match.group("unit"))
        if _plausible_price(value):
            return value, match.group(0).strip()

    # 4) Context phrase + plain number ("asking 450000", "budget of 380,000")
    has_price_context = any(word in lowered for word in _PRICE_HINT_WORDS)
    for match in _CEILING_RE.finditer(working):
        value = _amount(match.group("num"), match.group("unit"))
        if has_price_context and _plausible_price(value):
            return value, match.group(0).strip()

    # 5) Bare number — only when we asked for the price and it is unambiguous
    if slot_hint == "price":
        for match in _BARE_NUMBER_RE.finditer(working):
            raw = match.group("num")
            if "." in raw:
                continue
            value = _amount(raw, None)
            if value is None:
                continue
            if 100 <= value <= 9_999 and "," not in raw:
                value *= 1_000  # "450" in answer to a price question
            if _plausible_price(value):
                return value, match.group(0).strip()

    return None, None
def extract_condition(text: str) -> tuple[PropertyCondition | None, str | None]:
    lowered = _clean(text).lower()
    if not lowered:
        return None, None

    for term in _FULL_REHAB_TERMS:
        if term in lowered:
            return PropertyCondition.FULL_REHAB, term
    for term in _MINOR_REPAIRS_TERMS:
        if term in lowered:
            return PropertyCondition.MINOR_REPAIRS, term
    for term in _TURNKEY_TERMS:
        if term in lowered:
            if f"not {term}" in lowered or f"isn't {term}" in lowered:
                continue
            return PropertyCondition.TURNKEY, term
    return None, None


# --------------------------------------------------------------------------- #
# Size
# --------------------------------------------------------------------------- #
def extract_size(text: str, *, slot_hint: str | None = None) -> tuple[int | None, str | None]:
    cleaned = _clean(text)

    # Acres are common in US listings: 0.25 acre ~ 10,890 sq ft.
    for match in _ACRES_RE.finditer(cleaned):
        try:
            acres = float(match.group("num"))
        except ValueError:
            continue
        sqft = int(round(acres * 43_560))
        if SIZE_MIN <= sqft <= SIZE_MAX:
            return sqft, match.group(0).strip()

    for pattern in (_SQFT_RE, _SQFT_HYPHEN_RE):
        for match in pattern.finditer(cleaned):
            value = _amount(match.group("num"), match.group("unit"))
            if value is not None and SIZE_MIN <= value <= SIZE_MAX:
                return value, match.group(0).strip()

    if slot_hint == "size":
        for match in _BARE_NUMBER_RE.finditer(cleaned):
            raw = match.group("num")
            if "." in raw:
                continue
            value = _amount(raw, None)
            if value is None:
                continue
            if value < 100 and re.search(rf"{re.escape(raw)}\s*k\b", cleaned, re.IGNORECASE):
                value *= 1_000
            if SIZE_MIN <= value <= SIZE_MAX:
                return value, match.group(0).strip()

    return None, None


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def extract_slots(text: str, *, slot_hint: str | None = None) -> ExtractedSlots:
    """Extract every slot present in one inbound message.

    Passing ``slot_hint`` (the question we just asked) enables the more
    permissive bare-number rules, e.g. answering "450" to the price question.
    """
    if not text or not text.strip():
        return ExtractedSlots()

    price, price_evidence = extract_price(text, slot_hint=slot_hint)
    condition, condition_raw = extract_condition(text)
    size, size_evidence = extract_size(text, slot_hint=slot_hint)

    confidence = 0.0
    if price is not None:
        has_scale = bool(re.search(r"\d\s*(k|m|million|thousand)", text, re.I))
        confidence += 0.45 if ("$" in text or has_scale) else 0.3
    if condition is not None:
        confidence += 0.35
    if size is not None:
        confidence += 0.45 if re.search(r"(sq|square|ft|feet|acre)", text, re.I) else 0.3

    found = price is not None or condition is not None or size is not None
    return ExtractedSlots(
        price_usd=price,
        price_evidence=price_evidence,
        condition=condition,
        condition_raw=condition_raw,
        size_sqft=size,
        size_evidence=size_evidence,
        confidence=min(round(confidence, 2), 1.0),
        source="heuristic" if found else "none",
    )


def merge_extractions(primary: ExtractedSlots, secondary: ExtractedSlots | None) -> ExtractedSlots:
    """Merge two extractions, preferring ``primary`` (rule-based) values."""
    if secondary is None or secondary.is_empty():
        return primary

    merged = primary.model_copy(deep=True)
    if merged.price_usd is None and secondary.price_usd is not None:
        merged.price_usd = secondary.price_usd
        merged.price_evidence = secondary.price_evidence
    if merged.condition is None and secondary.condition is not None:
        merged.condition = secondary.condition
        merged.condition_raw = secondary.condition_raw
    if merged.size_sqft is None and secondary.size_sqft is not None:
        merged.size_sqft = secondary.size_sqft
        merged.size_evidence = secondary.size_evidence

    merged.confidence = max(primary.confidence, secondary.confidence)
    merged.source = "merged" if not merged.is_empty() else "none"
    return merged


def extraction_payload(slots: ExtractedSlots) -> dict[str, Any]:
    """JSON-safe view for the audit trail."""
    return slots.model_dump(mode="json")

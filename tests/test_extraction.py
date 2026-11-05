"""Slot extraction: prices, conditions, square footage, robustness."""

from __future__ import annotations

import pytest

from app.agent.extraction import (
    extract_condition,
    extract_price,
    extract_size,
    extract_slots,
    merge_extractions,
)
from app.domain.enums import PropertyCondition
from app.domain.schemas import ExtractedSlots


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$450,000", 450_000),
        ("450k", 450_000),
        ("I'd take 450 thousand", 450_000),
        ("looking for around $1.2 million", 1_200_000),
        ("1.2m works", 1_200_000),
        ("somewhere between 400k and 450k", 425_000),
        ("$399,900 firm", 399_900),
        ("up to 500k", 500_000),
        ("asking 385000", 385_000),
        ("I want 500000 dollars", 500_000),
        ("Maybe 625 grand", 625_000),
    ],
)
def test_extract_price_variants(text: str, expected: int) -> None:
    price, evidence = extract_price(text)
    assert price == expected
    assert evidence


@pytest.mark.parametrize(
    "text",
    [
        "It's a 3 bed 2 bath house",
        "2000 sq ft",
        "not sure yet",
        "",
        "2 acres",
    ],
)
def test_price_is_not_extracted_from_non_price_text(text: str) -> None:
    price, _ = extract_price(text)
    assert price is None


def test_bare_number_only_when_price_was_asked() -> None:
    assert extract_price("450", slot_hint=None)[0] is None
    assert extract_price("450", slot_hint="price")[0] == 450_000
    assert extract_price("485000", slot_hint="price")[0] == 485_000


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("it's turnkey", PropertyCondition.TURNKEY),
        ("move-in ready", PropertyCondition.TURNKEY),
        ("fully renovated last year", PropertyCondition.TURNKEY),
        ("needs minor cosmetic repairs", PropertyCondition.MINOR_REPAIRS),
        ("just some paint and carpet", PropertyCondition.MINOR_REPAIRS),
        ("needs a full rehab", PropertyCondition.FULL_REHAB),
        ("total gut job, down to the studs", PropertyCondition.FULL_REHAB),
        ("it's a fixer upper", PropertyCondition.FULL_REHAB),
        ("not turnkey, it needs major repairs", PropertyCondition.FULL_REHAB),
        ("not sure", None),
    ],
)
def test_extract_condition(text: str, expected: PropertyCondition | None) -> None:
    condition, _ = extract_condition(text)
    assert condition == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2000 sq ft", 2_000),
        ("1,850 square feet", 1_850),
        ("about 2.5k sqft", 2_500),
        ("it's 1,200-square-foot", 1_200),
        ("0.25 acres", 10_890),
        ("roughly 1600 sf", 1_600),
    ],
)
def test_extract_size(text: str, expected: int) -> None:
    size, evidence = extract_size(text)
    assert size == expected
    assert evidence


def test_bare_size_only_when_size_was_asked() -> None:
    assert extract_size("1800", slot_hint=None)[0] is None
    assert extract_size("1800", slot_hint="size")[0] == 1_800


def test_multiple_slots_in_one_message() -> None:
    slots = extract_slots(
        "It's a 2,000 sq ft rental, needs a full rehab, and I want $450k for it.",
        slot_hint="price",
    )
    assert slots.price_usd == 450_000
    assert slots.condition is PropertyCondition.FULL_REHAB
    assert slots.size_sqft == 2_000
    assert slots.confidence > 0.9
    assert slots.source == "heuristic"


def test_extract_slots_empty_message() -> None:
    slots = extract_slots("   ")
    assert slots.is_empty()
    assert slots.source == "none"


def test_price_not_taken_from_square_footage() -> None:
    slots = extract_slots("the house is 2,400 sq ft", slot_hint="size")
    assert slots.price_usd is None
    assert slots.size_sqft == 2_400


def test_merge_prefers_primary_and_fills_gaps() -> None:
    primary = ExtractedSlots(price_usd=450_000, source="heuristic", confidence=0.45)
    secondary = ExtractedSlots(
        price_usd=999_999,
        condition=PropertyCondition.TURNKEY,
        size_sqft=2_000,
        source="llm",
        confidence=0.6,
    )
    merged = merge_extractions(primary, secondary)
    assert merged.price_usd == 450_000  # rules win
    assert merged.condition is PropertyCondition.TURNKEY
    assert merged.size_sqft == 2_000
    assert merged.source == "merged"


def test_merge_with_nothing_returns_primary() -> None:
    primary = ExtractedSlots(price_usd=450_000, source="heuristic")
    assert merge_extractions(primary, None) == primary
    assert merge_extractions(primary, ExtractedSlots()) == primary

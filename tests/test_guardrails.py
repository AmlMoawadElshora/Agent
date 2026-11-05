"""Guardrail detection (opt-out, human request, negotiation, legal)."""

from __future__ import annotations

import pytest

from app.agent.guardrails import evaluate


@pytest.mark.parametrize(
    "text",
    ["stop", "STOP", "please stop messaging me", "unsubscribe me", "not interested",
     "remove my number", "don't message me again"],
)
def test_opt_out_detected(text: str) -> None:
    result = evaluate(text)
    assert result.opt_out is True
    assert result.reason == "opt_out"


@pytest.mark.parametrize(
    "text",
    ["can we negotiate the price?", "that's too low, come down a bit",
     "what's your best offer", "any discount?"],
)
def test_negotiation_detected(text: str) -> None:
    result = evaluate(text)
    assert result.negotiation is True
    assert result.reason == "price_negotiation"


@pytest.mark.parametrize(
    "text",
    ["is this legally binding?", "should I talk to an attorney?", "give me legal advice"],
)
def test_legal_detected(text: str) -> None:
    result = evaluate(text)
    assert result.legal is True
    assert result.reason == "legal"


@pytest.mark.parametrize(
    "text",
    ["can you call me", "I want to speak to a human", "let me talk to a manager"],
)
def test_human_request_detected(text: str) -> None:
    result = evaluate(text)
    assert result.human_requested is True
    assert result.reason == "human_handoff"


@pytest.mark.parametrize(
    "text",
    ["", "450k", "it needs a full rehab", "2000 sq ft", "the stopwatch is broken"],
)
def test_normal_messages_trigger_nothing(text: str) -> None:
    result = evaluate(text)
    assert result.triggered is False
    assert result.reason is None

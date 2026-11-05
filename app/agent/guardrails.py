"""Intent / guardrail detection for inbound messages.

The agent must never negotiate price, give legal advice, or keep messaging a
prospect who asked to stop.  Those checks live here as cheap deterministic
rules so they cannot be "talked out of" by a clever prompt injection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agent.extraction import (
    _HUMAN_TERMS,
    _LEGAL_TERMS,
    _NEGOTIATION_TERMS,
    _OPT_OUT_TERMS,
)


def _find(text: str, terms: tuple[str, ...]) -> str | None:
    """Return the first term present in ``text``.

    Single-word terms use word boundaries (so ``stop`` does not match
    ``stopwatch``), multi-word terms are matched as plain substrings.
    """
    lowered = text.lower()
    for term in terms:
        if " " in term or "-" in term:
            if term in lowered:
                return term
        elif re.search(rf"\b{re.escape(term)}\b", lowered):
            return term
    return None


@dataclass(slots=True)
class GuardrailResult:
    opt_out: bool = False
    human_requested: bool = False
    negotiation: bool = False
    legal: bool = False
    matched: str | None = None

    @property
    def triggered(self) -> bool:
        return self.opt_out or self.human_requested or self.negotiation or self.legal

    @property
    def reason(self) -> str | None:
        if self.opt_out:
            return "opt_out"
        if self.legal:
            return "legal"
        if self.human_requested:
            return "human_handoff"
        if self.negotiation:
            return "price_negotiation"
        return None


def evaluate(text: str) -> GuardrailResult:
    """Classify an inbound message against the safety rules."""
    if not text or not text.strip():
        return GuardrailResult()

    opt_out = _find(text, _OPT_OUT_TERMS)
    legal = _find(text, _LEGAL_TERMS)
    negotiation = _find(text, _NEGOTIATION_TERMS)
    human = _find(text, _HUMAN_TERMS)

    return GuardrailResult(
        opt_out=bool(opt_out),
        human_requested=bool(human),
        negotiation=bool(negotiation),
        legal=bool(legal),
        matched=opt_out or legal or human or negotiation,
    )

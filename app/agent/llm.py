"""Optional LLM layer (OpenAI-compatible chat completions).

The agent is fully functional with ``LLM_ENABLED=false`` — the deterministic
state machine then composes every message.  When an LLM is configured it is
used for two narrow, low-risk jobs:

1. Extracting slots from unusual phrasings the regexes miss.
2. Rewriting an already-decided reply so it sounds more natural.

In both cases the deterministic result is the source of truth: the model can
only *fill gaps* or *reword*, never change state, skip a question, or add
commitments.  Any error/timeout silently falls back to the templates.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.agent import prompts
from app.config import Settings, get_settings
from app.domain.enums import PropertyCondition
from app.domain.schemas import ExtractedSlots, LeadContext

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class LLMClient:
    """Thin, defensive wrapper around an OpenAI-compatible ``/chat/completions``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.Client | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.llm_enabled and self.settings.openai_api_key)

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.settings.openai_base_url.rstrip("/"),
                timeout=self.settings.llm_timeout_seconds,
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> str | None:
        if not self.enabled:
            return None

        payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(1, self.settings.llm_max_retries + 2):
            try:
                response = self._get_client().post("/chat/completions", json=payload)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if isinstance(content, str) and content.strip():
                    return content.strip()
                logger.warning("llm returned empty content", extra={"attempt": attempt})
            except Exception as exc:  # noqa: BLE001 - never break the conversation
                logger.warning(
                    "llm request failed",
                    extra={
                        "attempt": attempt,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:200],
                    },
                )
        return None


    def extract_slots(self, text: str) -> ExtractedSlots | None:
        """Ask the model for structured slots; returns ``None`` on any problem."""
        if not self.enabled or not text.strip():
            return None

        content = self._chat(
            prompts.EXTRACTION_SYSTEM_PROMPT,
            f'Prospect message: "{text}"',
            json_mode=True,
            temperature=0.0,
        )
        if not content:
            return None

        match = _JSON_BLOCK.search(content)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("llm extraction returned invalid JSON")
            return None

        try:
            condition = data.get("condition")
            slots = ExtractedSlots(
                price_usd=_as_int(data.get("price_usd")),
                condition=PropertyCondition(condition) if condition else None,
                size_sqft=_as_int(data.get("size_sqft")),
                confidence=0.6,
                source="llm",
            )
        except (ValueError, TypeError) as exc:
            logger.warning("llm extraction failed validation", extra={"error": str(exc)})
            return None
        return None if slots.is_empty() else slots

    def rewrite_reply(
        self,
        context: LeadContext,
        draft: str,
        *,
        next_slot: str | None,
        history: list[dict[str, str]] | None = None,
    ) -> str | None:
        """Politely reword ``draft``.  Returns ``None`` to keep the draft."""
        if not self.enabled:
            return None

        lines = [
            f"Known details: {context.collected() or 'none yet'}",
            (
                "Next question to ask verbatim: "
                + (
                    prompts.question_for(next_slot, 0)
                    if next_slot
                    else "NONE - do not ask a question"
                )
            ),
            f"Draft reply: {draft}",
        ]
        if history:
            recent = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
            lines.insert(0, f"Recent conversation:\n{recent}")

        rewritten = self._chat(
            prompts.SYSTEM_PROMPT + prompts.REPLY_SYSTEM_ADDENDUM,
            "\n".join(lines),
            temperature=0.4,
        )
        return _validate_reply(rewritten, next_slot=next_slot)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _validate_reply(reply: str | None, *, next_slot: str | None) -> str | None:
    """Reject unsafe / malformed model output and fall back to the draft."""
    if not reply:
        return None
    cleaned = reply.strip().strip('"').strip()
    if not cleaned or len(cleaned) > 700:
        return None
    if "as an ai language model" in cleaned.lower():
        return None
    if next_slot and "?" not in cleaned:
        return None
    return cleaned


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm_client() -> None:
    global _client
    if _client is not None:
        _client.close()
    _client = None

"""Application settings.

All configuration is environment driven (12-factor).  A local ``.env`` file is
read automatically when present, which keeps development friction low while
still allowing a container scheduler to inject real environment variables.

``get_settings`` is cached, so the whole process shares one immutable config
object.  Tests call ``get_settings.cache_clear()`` after overriding the
environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.phones import normalize_phone


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "real-estate-whatsapp-agent"
    environment: Literal["development", "staging", "production", "test"] = "development"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"

    # ------------------------------------------------------------- database
    database_url: str = "sqlite:///./data/agent.db"
    db_echo: bool = False
    auto_create_tables: bool = True
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---------------------------------------------------------------- redis
    # Optional.  When unset the app falls back to an in-process lock map,
    # which is fine for a single worker.  Use Redis for multi-worker setups.
    redis_url: str | None = None

    # --------------------------------------------------------------- twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    # Sandbox default: the Twilio WhatsApp sandbox sender.
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    # Signature validation must stay enabled in production.  It is disabled
    # automatically when no auth token is configured (local dev / tests).
    validate_twilio_signature: bool = True
    # "twiml" replies inside the webhook response (no extra API call),
    # "api" sends the message through the Twilio REST API instead.
    reply_mode: Literal["twiml", "api"] = "twiml"
    # Global kill-switch: when true messages are logged, never transmitted.
    dry_run: bool = True
    outbound_rate_limit_per_second: float = 1.0

    # ------------------------------------------------------- admin / handoff
    # The acquisitions manager that receives qualified lead cards.
    admin_whatsapp_number: str = "01020645167"
    admin_api_key: str | None = None
    handoff_notify_lead: bool = True

    # ------------------------------------------------------------------ llm
    # Any OpenAI-compatible endpoint works (OpenAI, Azure, Groq, OpenRouter,
    # Ollama...).  When disabled the deterministic responder is used, which is
    # fully functional and is what the test-suite exercises.
    llm_enabled: bool = False
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.3

    # ---------------------------------------------------------------- agent
    default_country_code: str = "1"
    # Slots are always asked one at a time, in this order.
    question_order: str = "price,condition,size"
    # After N attempts on the same slot we stop re-asking and hand the lead to
    # a human instead of looping forever.
    max_questions_per_slot: int = 3
    # Twilio retries webhooks; anything older than this is rejected.
    webhook_max_skew_seconds: int = 300

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("admin_whatsapp_number", "twilio_whatsapp_from")
    @classmethod
    def _normalize_numbers(cls, value: str) -> str:
        """Store every phone number in E.164 so comparisons never fail."""
        if not value:
            return value
        prefix = ""
        candidate = value
        if candidate.startswith("whatsapp:"):
            prefix = "whatsapp:"
            candidate = candidate[len(prefix) :]
        return f"{prefix}{normalize_phone(candidate)}"

    # ----------------------------------------------------------- helpers
    @property
    def normalized_twilio_from(self) -> str:
        return self.twilio_whatsapp_from

    @property
    def admin_phone_e164(self) -> str:
        return self.admin_whatsapp_number.replace("whatsapp:", "")

    @property
    def twilio_from_e164(self) -> str:
        return self.twilio_whatsapp_from.replace("whatsapp:", "")

    @property
    def ordered_slots(self) -> list[str]:
        slots = [s.strip().lower() for s in self.question_order.split(",") if s.strip()]
        valid = [s for s in slots if s in {"price", "condition", "size"}]
        # Defensive: always cover every slot exactly once.
        for fallback in ("price", "condition", "size"):
            if fallback not in valid:
                valid.append(fallback)
        return valid

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def signature_validation_active(self) -> bool:
        return self.validate_twilio_signature and bool(self.twilio_auth_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()

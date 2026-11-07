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

    # ------------------------------------------------- meta whatsapp cloud api
    # Official Meta WhatsApp Cloud API (Graph API) credentials.
    meta_access_token: str | None = None
    meta_phone_number_id: str | None = None
    # Optional: WhatsApp Business Account id, used for logging/ops only.
    meta_waba_id: str | None = None
    # Echoed back during the webhook verification handshake (hub.verify_token).
    meta_verify_token: str = "change-me-verify-token"
    # App secret from the Meta app dashboard; enables X-Hub-Signature-256 checks.
    meta_app_secret: str | None = None
    meta_api_version: str = "v18.0"
    meta_graph_base_url: str = "https://graph.facebook.com"
    meta_request_timeout_seconds: float = 15.0
    meta_max_retries: int = 2
    # Approved template used for the business-initiated campaign opener.
    # Leave empty to send a plain text opener (only valid inside a 24h window).
    meta_welcome_template_name: str | None = None
    meta_template_language: str = "en_US"
    # Comma separated template body variables rendered from the lead, e.g.
    # "first_name,company". Supported tokens: first_name, full_name, agent,
    # company, market. Empty = template without variables.
    meta_template_params: str = ""
    # Signature validation must stay enabled in production.  It is disabled
    # automatically when no app secret is configured (local dev / tests).
    validate_meta_signature: bool = True
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
    # Meta retries webhooks; anything older than this is rejected.
    webhook_max_skew_seconds: int = 300

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("admin_whatsapp_number")
    @classmethod
    def _normalize_numbers(cls, value: str) -> str:
        """Store every phone number in E.164 so comparisons never fail."""
        if not value:
            return value
        return normalize_phone(value.replace("whatsapp:", ""))

    # ----------------------------------------------------------- helpers
    @property
    def admin_phone_e164(self) -> str:
        return self.admin_whatsapp_number.replace("whatsapp:", "")

    @property
    def admin_recipient(self) -> str:
        """Cloud API addressing: digits only, no leading ``+``."""
        return self.admin_phone_e164.lstrip("+")

    @property
    def meta_messages_url(self) -> str:
        """``https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages``."""
        base = self.meta_graph_base_url.rstrip("/")
        return f"{base}/{self.meta_api_version}/{self.meta_phone_number_id}/messages"

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
    def template_params(self) -> list[str]:
        return [p.strip().lower() for p in self.meta_template_params.split(",") if p.strip()]

    @property
    def meta_configured(self) -> bool:
        return bool(self.meta_access_token and self.meta_phone_number_id)

    @property
    def signature_validation_active(self) -> bool:
        return self.validate_meta_signature and bool(self.meta_app_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()

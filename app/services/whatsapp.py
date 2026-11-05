"""WhatsApp transport abstraction.

The rest of the codebase only knows the :class:`WhatsAppSender` protocol, which
makes it trivial to run dry (log only), use Twilio, or swap in Meta's Cloud API
later.  Sending never raises: failures come back as a ``SendResult`` so one bad
number cannot break a campaign or a webhook.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings, get_settings
from app.domain.phones import mask_phone, normalize_phone, to_whatsapp_uri

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SendResult:
    ok: bool
    sid: str | None = None
    status: str | None = None
    error: str | None = None
    dry_run: bool = False


class WhatsAppSender(Protocol):
    dry_run: bool

    def send_whatsapp(self, to: str, body: str) -> SendResult: ...


class ConsoleSender:
    """Dry-run sender: logs the message, never touches the network."""

    dry_run = True

    def __init__(self) -> None:
        self.outbox: list[dict[str, str]] = []

    def send_whatsapp(self, to: str, body: str) -> SendResult:
        sid = f"DRYRUN-{uuid.uuid4().hex[:16]}"
        self.outbox.append({"to": normalize_phone(to), "body": body, "sid": sid})
        logger.info(
            "whatsapp[DRY-RUN] -> %s | %s",
            mask_phone(to),
            body[:120].replace("\n", " "),
        )
        return SendResult(ok=True, sid=sid, status="dry_run", dry_run=True)


class TwilioSender:
    """Twilio Programmable Messaging sender (WhatsApp channel)."""

    dry_run = False

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.twilio_configured:
            raise RuntimeError(
                "Twilio is not configured: set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN"
            )
        # Imported lazily so the package is optional in dry-run deployments.
        from twilio.rest import Client

        self._client = Client(
            self.settings.twilio_account_sid,
            self.settings.twilio_auth_token,
        )

    def send_whatsapp(self, to: str, body: str) -> SendResult:
        target = to_whatsapp_uri(to)
        try:
            message = self._client.messages.create(
                from_=self.settings.normalized_twilio_from,
                to=target,
                body=body,
            )
        except Exception as exc:  # noqa: BLE001 - Twilio raises many types
            code = getattr(exc, "code", None)
            logger.error(
                "twilio send failed",
                extra={
                    "to": mask_phone(to),
                    "error": type(exc).__name__,
                    "code": code,
                    "detail": str(exc)[:300],
                },
            )
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])

        logger.info(
            "whatsapp sent",
            extra={"to": mask_phone(to), "sid": message.sid, "status": message.status},
        )
        return SendResult(ok=True, sid=message.sid, status=message.status or "queued")


class SenderRouter:
    """Chooses the real sender or the dry-run sender based on configuration."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._twilio: TwilioSender | None = None
        self.console = ConsoleSender()

    @property
    def dry_run(self) -> bool:
        return self.settings.dry_run or not self.settings.twilio_configured

    def send_whatsapp(self, to: str, body: str) -> SendResult:
        if self.dry_run:
            return self.console.send_whatsapp(to, body)
        try:
            if self._twilio is None:
                self._twilio = TwilioSender(self.settings)
            return self._twilio.send_whatsapp(to, body)
        except Exception as exc:  # noqa: BLE001 - never crash the request
            logger.error("whatsapp transport unavailable: %s", exc)
            return SendResult(ok=False, error=f"transport unavailable: {exc}"[:500])


_sender: SenderRouter | None = None


def get_sender() -> SenderRouter:
    global _sender
    if _sender is None:
        _sender = SenderRouter()
    return _sender


def set_sender(sender: SenderRouter | None) -> None:
    """Test hook to inject a sender (or reset it)."""
    global _sender
    _sender = sender

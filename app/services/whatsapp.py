"""WhatsApp transport: Meta WhatsApp Cloud API (Graph API).

The rest of the codebase only knows the :class:`WhatsAppSender` protocol, so
the transport can be swapped without touching the agent, the state machine or
the database models.

Sending is done over plain HTTPS against::

    POST https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages
    Authorization: Bearer {META_ACCESS_TOKEN}

Two message types are supported:

* ``text``     — free-form replies, only valid inside the 24-hour customer
                 service window (all inbound-driven conversation turns).
* ``template`` — pre-approved templates, required for business-initiated
                 messages such as the campaign opener.

Sending never raises: failures come back as a :class:`SendResult` with the
Graph API error code and message so one bad number cannot break a campaign or a
webhook.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.agent.prompts import AGENT_NAME, COMPANY_NAME, MARKET, first_name
from app.config import Settings, get_settings
from app.domain.phones import mask_phone, normalize_phone, to_meta_recipient

logger = logging.getLogger(__name__)

# Retry these HTTP status codes; everything else is a permanent error.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class SendResult:
    ok: bool
    sid: str | None = None          # Meta message id ("wamid....")
    status: str | None = None
    error: str | None = None
    dry_run: bool = False
    message_type: str = "text"
    raw: dict[str, Any] = field(default_factory=dict)


class WhatsAppSender(Protocol):
    dry_run: bool

    def send_whatsapp(self, to: str, body: str) -> SendResult: ...


class ConsoleSender:
    """Dry-run sender: logs the message, never touches the network."""

    dry_run = True

    def __init__(self) -> None:
        self.outbox: list[dict[str, Any]] = []

    def send_whatsapp(self, to: str, body: str) -> SendResult:
        sid = f"DRYRUN-{uuid.uuid4().hex[:16]}"
        self.outbox.append(
            {"to": normalize_phone(to), "body": body, "sid": sid, "type": "text"}
        )
        logger.info(
            "whatsapp[DRY-RUN] -> %s | %s",
            mask_phone(to),
            body[:120].replace("\n", " "),
        )
        return SendResult(ok=True, sid=sid, status="dry_run", dry_run=True)

    def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en_US",
        parameters: list[str] | None = None,
        *,
        fallback_text: str = "",
    ) -> SendResult:
        sid = f"DRYRUN-{uuid.uuid4().hex[:16]}"
        rendered = fallback_text or f"[template:{template_name}] " + " ".join(parameters or [])
        self.outbox.append(
            {
                "to": normalize_phone(to),
                "body": rendered,
                "sid": sid,
                "type": "template",
                "template": template_name,
                "language": language_code,
                "parameters": list(parameters or []),
            }
        )
        logger.info(
            "whatsapp[DRY-RUN][template:%s] -> %s | %s",
            template_name,
            mask_phone(to),
            rendered[:120].replace("\n", " "),
        )
        return SendResult(
            ok=True,
            sid=sid,
            status="dry_run",
            dry_run=True,
            message_type="template",
            raw={"template": template_name, "parameters": list(parameters or [])},
        )


class MetaWhatsAppSender:
    """Direct client for the official WhatsApp Cloud API."""

    dry_run = False

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.meta_configured:
            raise RuntimeError(
                "Meta WhatsApp is not configured: set META_ACCESS_TOKEN and "
                "META_PHONE_NUMBER_ID"
            )
        self._client = client or httpx.Client(
            timeout=self.settings.meta_request_timeout_seconds,
            headers={
                "Authorization": f"Bearer {self.settings.meta_access_token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ #
    def _post(self, payload: dict[str, Any]) -> SendResult:
        url = self.settings.meta_messages_url
        attempts = max(self.settings.meta_max_retries, 0) + 1
        last_error = "meta request failed"

        for attempt in range(1, attempts + 1):
            try:
                response = self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "meta request error",
                    extra={"attempt": attempt, "error": last_error[:200]},
                )
                if attempt < attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))
                    continue
                return SendResult(ok=False, error=last_error[:500])

            if response.status_code < 400:
                return self._success(response)

            error, retryable = _parse_error(response)
            last_error = error
            logger.warning(
                "meta send failed",
                extra={
                    "attempt": attempt,
                    "status": response.status_code,
                    "error": error[:200],
                },
            )
            if retryable and attempt < attempts:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if (retry_after or "").isdigit()
                    else 2 ** (attempt - 1)
                )
                time.sleep(min(delay, 8))
                continue
            return SendResult(ok=False, error=error[:500], status=str(response.status_code))

        return SendResult(ok=False, error=last_error[:500])

    @staticmethod
    def _success(response: httpx.Response) -> SendResult:
        try:
            data = response.json()
        except ValueError:
            data = {}
        messages = data.get("messages") or []
        first = messages[0] if messages else {}
        return SendResult(
            ok=True,
            sid=first.get("id"),
            status=first.get("message_status") or "accepted",
            raw=data,
        )

    # ------------------------------------------------------------------ #
    def send_whatsapp(self, to: str, body: str) -> SendResult:
        """Send a free-form text message (requires an open 24h window)."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_meta_recipient(to),
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        result = self._post(payload)
        if result.ok:
            logger.info(
                "whatsapp text sent",
                extra={"to": mask_phone(to), "sid": result.sid, "status": result.status},
            )
        else:
            logger.error(
                "whatsapp text failed",
                extra={"to": mask_phone(to), "error": result.error},
            )
        return result

    def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en_US",
        parameters: list[str] | None = None,
        *,
        fallback_text: str = "",
    ) -> SendResult:
        """Send an approved template (business-initiated / campaign opener)."""
        del fallback_text  # only used by the dry-run sender
        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        params = list(parameters or [])
        if params:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": value} for value in params],
                }
            ]

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_meta_recipient(to),
            "type": "template",
            "template": template,
        }
        result = self._post(payload)
        result.message_type = "template"
        if result.ok:
            logger.info(
                "whatsapp template sent",
                extra={
                    "to": mask_phone(to),
                    "template": template_name,
                    "sid": result.sid,
                    "status": result.status,
                },
            )
        else:
            logger.error(
                "whatsapp template failed",
                extra={
                    "to": mask_phone(to),
                    "template": template_name,
                    "error": result.error,
                },
            )
        return result


def _parse_error(response: httpx.Response) -> tuple[str, bool]:
    """Return ``(human readable error, retryable)`` for a Graph API failure."""
    message = ""
    code: Any = None
    subcode: Any = None
    trace: Any = None
    try:
        body = response.json()
        error = body.get("error") or {}
        message = str(error.get("message") or "")
        code = error.get("code")
        subcode = error.get("error_subcode")
        trace = error.get("fbtrace_id")
    except ValueError:
        message = response.text[:200]

    parts = [f"meta {response.status_code}"]
    if code is not None:
        parts.append(f"code={code}")
    if subcode is not None:
        parts.append(f"subcode={subcode}")
    detail = " ".join(parts)
    if message:
        detail = f"{detail}: {message}"
    if trace:
        detail = f"{detail} (fbtrace_id={trace})"

    retryable = response.status_code in _RETRYABLE_STATUS
    # Meta's 131000-family errors are transient platform issues.
    if code in {131000, 131016, 131042} or subcode == 2494010:
        retryable = True
    return detail, retryable


class SenderRouter:
    """Chooses the real Meta sender or the dry-run sender based on config."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._meta: MetaWhatsAppSender | None = None
        self.console = ConsoleSender()

    @property
    def dry_run(self) -> bool:
        return self.settings.dry_run or not self.settings.meta_configured

    @property
    def supports_templates(self) -> bool:
        return bool(self.settings.meta_welcome_template_name)

    def send_whatsapp(self, to: str, body: str) -> SendResult:
        if self.dry_run:
            return self.console.send_whatsapp(to, body)
        try:
            if self._meta is None:
                self._meta = MetaWhatsAppSender(self.settings)
            return self._meta.send_whatsapp(to, body)
        except Exception as exc:  # noqa: BLE001 - never crash the request
            logger.error("whatsapp transport unavailable: %s", exc)
            return SendResult(ok=False, error=f"transport unavailable: {exc}"[:500])

    def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en_US",
        parameters: list[str] | None = None,
        *,
        fallback_text: str = "",
    ) -> SendResult:
        if self.dry_run:
            return self.console.send_template(
                to,
                template_name,
                language_code,
                parameters,
                fallback_text=fallback_text,
            )
        try:
            if self._meta is None:
                self._meta = MetaWhatsAppSender(self.settings)
            return self._meta.send_template(
                to,
                template_name,
                language_code,
                parameters,
                fallback_text=fallback_text,
            )
        except Exception as exc:  # noqa: BLE001 - never crash the request
            logger.error("whatsapp transport unavailable: %s", exc)
            return SendResult(
                ok=False, error=f"transport unavailable: {exc}"[:500], message_type="template"
            )


def render_template_parameters(settings: Settings, lead: Any) -> list[str]:
    """Render ``META_TEMPLATE_PARAMS`` tokens for one lead.

    Supported tokens: ``first_name``, ``full_name``, ``agent``, ``company``,
    ``market``. Unknown tokens render as an empty string so a typo cannot break
    a whole campaign loudly.
    """
    name = getattr(lead, "full_name", None)
    values = {
        "first_name": first_name(name) or "there",
        "full_name": name or "there",
        "agent": AGENT_NAME,
        "company": COMPANY_NAME,
        "market": MARKET,
    }
    return [values.get(token, "") for token in settings.template_params]


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



"""Phone-number normalisation.

WhatsApp expects E.164 (``+<country><national number>``).  The prospect lists
we ingest are messy: they contain ``whatsapp:`` prefixes, spaces, dashes,
brackets and — very commonly — a national trunk zero (``01020645167``).

``normalize_phone`` converts all of that to a single canonical form so the
same human never ends up with two lead rows.  It raises ``PhoneNumberError``
for anything that cannot be a phone number, which the API/campaign layer turns
into a clear 422 response.
"""

from __future__ import annotations

import re

_WHATSAPP_PREFIX = re.compile(r"^whatsapp:", re.IGNORECASE)
_ALLOWED = re.compile(r"^\+?[0-9]+$")


class PhoneNumberError(ValueError):
    """Raised when a value cannot be interpreted as a phone number."""


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


def normalize_phone(raw: str | None, default_country_code: str = "1") -> str:
    """Return ``raw`` as an E.164 string (``+14155550123``).

    Rules (in order):
      1. Strip whitespace / ``whatsapp:`` prefix / punctuation (``-().``).
      2. ``00`` international prefix -> ``+``.
      3. A leading ``+`` is authoritative; only length is validated.
      4. 10 digits -> assume NANP (configurable country code).
      5. 11 digits starting with ``0`` -> national trunk prefix, drop it.
      6. 11-15 digits -> already includes a country code.
    """
    if raw is None:
        raise PhoneNumberError("phone number is required")

    value = _WHATSAPP_PREFIX.sub("", str(raw).strip())
    if not value:
        raise PhoneNumberError("phone number is empty")

    value = value.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    value = value.replace(".", "").replace("/", "")

    has_plus = value.startswith("+")
    if has_plus:
        value = value[1:]

    if not _ALLOWED.match(value if has_plus else f"0{value}"):
        raise PhoneNumberError(f"invalid characters in phone number: {raw!r}")

    if value.startswith("00"):
        value = value[2:]
        has_plus = True

    if not value.isdigit():
        raise PhoneNumberError(f"invalid phone number: {raw!r}")

    cc = _digits(default_country_code) or "1"

    if has_plus:
        e164 = f"+{value}"
    elif len(value) == 10:
        e164 = f"+{cc}{value}"
    elif len(value) == 11 and value.startswith("0"):
        # National trunk prefix (e.g. 01020645167 -> 1020645167 -> +11020645167)
        e164 = f"+{cc}{value[1:]}"
    elif len(value) == 11 and value.startswith(cc) and cc == "1":
        e164 = f"+{value}"  # already NANP with country code, missing '+'
    elif 11 <= len(value) <= 15:
        e164 = f"+{value}"
    elif len(value) > 15:
        raise PhoneNumberError(f"phone number too long: {raw!r}")
    else:
        raise PhoneNumberError(
            f"cannot determine country code for {raw!r}; pass it in E.164 (e.g. +14155550123)"
        )

    digits = e164[1:]
    if not 8 <= len(digits) <= 15:
        raise PhoneNumberError(f"phone number has invalid length: {raw!r}")

    return e164


def to_meta_recipient(e164: str) -> str:
    """``+14155550123`` -> ``14155550123`` (Cloud API addressing).

    Meta's Graph API expects the recipient as digits only, including the country
    code and without a leading ``+`` or any ``whatsapp:`` prefix.
    """
    return normalize_phone(e164).lstrip("+")


def to_whatsapp_uri(e164: str) -> str:
    """``+14155550123`` -> ``whatsapp:+14155550123`` (deep links / QR codes)."""
    return f"whatsapp:{normalize_phone(e164)}"


def mask_phone(e164: str) -> str:
    """Partially redact a number for logs (keeps last 4 digits)."""
    number = e164.replace("whatsapp:", "")
    if len(number) <= 5:
        return "***"
    return f"{number[:2]}***{number[-4:]}"

"""Phone normalisation and WhatsApp addressing."""

from __future__ import annotations

import pytest

from app.domain.phones import (
    PhoneNumberError,
    mask_phone,
    normalize_phone,
    to_whatsapp_uri,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("01020645167", "+11020645167"),
        ("01131043304", "+11131043304"),
        ("01032547715", "+11032547715"),
        ("+1 (415) 555-0123", "+14155550123"),
        ("4155550123", "+14155550123"),
        ("14155550123", "+14155550123"),
        ("whatsapp:+14155550123", "+14155550123"),
        ("0044 20 7946 0958", "+442079460958"),
        ("+44 7700 900123", "+447700900123"),
        ("  1-415-555-0199  ", "+14155550199"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


def test_normalize_uses_default_country_code() -> None:
    assert normalize_phone("4155550123", "44") == "+444155550123"


@pytest.mark.parametrize(
    "raw",
    ["", None, "abc", "12345", "+12345678901234567890", "++15551234567"],
)
def test_normalize_rejects_garbage(raw: str | None) -> None:
    with pytest.raises(PhoneNumberError):
        normalize_phone(raw)


def test_whatsapp_uri() -> None:
    assert to_whatsapp_uri("01020645167") == "whatsapp:+11020645167"


def test_mask_phone_keeps_last_four() -> None:
    masked = mask_phone("+14155550123")
    assert masked.endswith("0123")
    assert "4155" not in masked

#!/usr/bin/env python3
"""Outbound campaign runner.

Examples
--------
Dry run (default — logs only)::

    python -m scripts.campaign --numbers 01020645167,01131043304,01032547715

Live run (requires DRY_RUN=false + Twilio credentials in the environment)::

    python -m scripts.campaign --numbers 01020645167,01131043304 --live

CSV import (columns: phone[,name][,source])::

    python -m scripts.campaign --file prospects.csv --live --limit 200
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from app.config import get_settings
from app.db.base import init_db, session_scope
from app.domain.schemas import LeadCreate
from app.logging_config import configure_logging
from app.services.campaign import launch_campaign
from app.services.whatsapp import SenderRouter

logger = logging.getLogger("campaign")

PHONE_HEADERS = {"phone", "phone_number", "number", "mobile", "whatsapp", "phone number"}
NAME_HEADERS = {"name", "full_name", "full name", "contact", "owner"}
SOURCE_HEADERS = {"source", "campaign", "list"}


def _normalise_header(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def load_csv(path: Path) -> list[LeadCreate]:
    """Read prospects from a CSV file with tolerant header detection."""
    if not path.exists():
        raise SystemExit(f"CSV file not found: {path}")

    leads: list[LeadCreate] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample.strip() else csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            raise SystemExit("CSV appears to be empty")

        mapping: dict[str, str] = {}
        for header in reader.fieldnames:
            key = _normalise_header(header or "")
            if key in PHONE_HEADERS:
                mapping.setdefault("phone", header)
            elif key in NAME_HEADERS:
                mapping.setdefault("name", header)
            elif key in SOURCE_HEADERS:
                mapping.setdefault("source", header)

        if "phone" not in mapping:
            # Fall back to the first column so simple one-column lists work.
            mapping["phone"] = reader.fieldnames[0]

        for row in reader:
            phone = (row.get(mapping["phone"]) or "").strip()
            if not phone:
                continue
            leads.append(
                LeadCreate(
                    phone_number=phone,
                    full_name=(row.get(mapping["name"]) or "").strip() or None
                    if "name" in mapping
                    else None,
                    source=(row.get(mapping["source"]) or "").strip() or None
                    if "source" in mapping
                    else None,
                )
            )
    return leads


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send the WhatsApp opening message to prospects")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--numbers", help="Comma-separated phone numbers")
    source.add_argument("--file", type=Path, help="CSV file of prospects")
    parser.add_argument("--source", default="campaign", help="Lead source label")
    parser.add_argument("--limit", type=int, default=None, help="Max prospects to message")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually transmit (requires DRY_RUN=false and Twilio credentials)",
    )
    parser.add_argument("--log-level", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(args.log_level or settings.log_level)

    if args.file:
        leads = load_csv(args.file)
    else:
        leads = [
            LeadCreate(phone_number=number.strip(), source=args.source)
            for number in (args.numbers or "").split(",")
            if number.strip()
        ]

    if not leads:
        logger.error("no prospects to message")
        return 2

    if args.limit:
        leads = leads[: args.limit]

    init_db()
    sender = SenderRouter(settings)

    if args.live and sender.dry_run:
        logger.error(
            "live send requested but transport is dry-run — set DRY_RUN=false and "
            "TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN"
        )
        return 3

    logger.info(
        "starting campaign",
        extra={"prospects": len(leads), "dry_run": not args.live},
    )

    with session_scope() as db:
        result = launch_campaign(
            db,
            leads,
            sender=sender,
            settings=settings,
            dry_run=not args.live,
            source=args.source,
        )

    print(
        f"\nCampaign complete: total={result.total} sent={result.sent} "
        f"skipped={result.skipped} failed={result.failed} dry_run={result.dry_run}\n"
    )
    for item in result.results:
        state = "SENT" if item.sent else ("FAILED" if item.error else "SKIPPED")
        detail = item.error or item.skipped_reason or ""
        print(f"  {item.phone_number:<18} {state:<8} {detail}")

    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

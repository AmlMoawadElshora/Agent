#!/usr/bin/env python3
"""Create the database schema (idempotent).

    python -m scripts.init_db

In production prefer migrations (Alembic) — ``sql/schema.sql`` contains the
equivalent DDL if you need to bootstrap PostgreSQL by hand.
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.db.base import init_db
from app.logging_config import configure_logging


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    print(f"Schema ready at {settings.database_url.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

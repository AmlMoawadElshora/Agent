"""Database engine / session management (SQLAlchemy 2.0)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _is_sqlite_memory(url: str) -> bool:
    """True for ``sqlite://``, ``sqlite:///:memory:`` and ``:memory:`` DSNs."""
    if not url.startswith("sqlite"):
        return False
    database = url.split("://", 1)[1] if "://" in url else ""
    return ":memory:" in url or database in {"", "/"}


def _build_engine(settings: Settings) -> Engine:
    url = settings.database_url
    kwargs: dict = {"echo": settings.db_echo, "future": True, "pool_pre_ping": True}

    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if _is_sqlite_memory(url):
            # One shared connection so every thread/request sees the same data.
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow

    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _create_all = None
        settings = get_settings()
        _engine = _build_engine(settings)
        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
        logger.info(
            "database engine initialised",
            extra={"database_url": _safe_url(settings.database_url)},
        )
        _ = _create_all
    return _engine


def _safe_url(url: str) -> str:
    """Strip credentials before logging a DSN."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts (commit on success, rollback on error)."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create tables if they do not exist (idempotent)."""
    from app.db import models  # noqa: F401  (register mappers)

    engine = get_engine()
    models.Base.metadata.create_all(bind=engine)
    logger.info("database schema ensured")


def reset_state() -> None:
    """Dispose the engine — used by tests to swap databases."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None

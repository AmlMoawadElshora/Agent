"""Per-lead mutual exclusion.

WhatsApp frequently delivers two messages within a few hundred milliseconds of
each other.  Without a lock the two webhook requests can both read the same
lead snapshot, ask the same question twice, or send two handoff cards.

Redis (``SET NX PX``) is used when ``REDIS_URL`` is configured; otherwise an
in-process ``threading.Lock`` map is used, which is correct for the single
worker / ``--workers 1`` deployment described in the README.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    import redis as redis_lib
except Exception:  # noqa: BLE001
    redis_lib = None  # type: ignore[assignment]


class _MemoryLockRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


_registry = _MemoryLockRegistry()
_redis_client = None


def _get_redis(settings: Settings):
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not settings.redis_url or redis_lib is None:
        return None
    try:
        _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        _redis_client.ping()
        logger.info("redis lock backend connected")
        return _redis_client
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis unavailable (%s); using in-process locks", exc)
        _redis_client = None
        return None


@contextmanager
def lead_lock(lead_key: str, *, ttl_seconds: int = 30, wait_seconds: float = 8.0) -> Iterator[bool]:
    """Acquire an exclusive lock for a lead.

    Yields ``True`` when the lock was acquired.  If it cannot be acquired in
    ``wait_seconds`` the caller still proceeds (best effort) — dropping a
    prospect's message is worse than a rare duplicate question.
    """
    settings = get_settings()
    key = f"lead-lock:{lead_key}"
    client = _get_redis(settings)

    if client is None:
        lock = _registry.get(key)
        acquired = lock.acquire(timeout=max(wait_seconds, 0.1))
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
        return

    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    acquired = False
    while time.monotonic() < deadline:
        try:
            acquired = bool(client.set(key, token, nx=True, ex=ttl_seconds))
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis lock error (%s); continuing without lock", exc)
            acquired = True
            break
        if acquired:
            break
        time.sleep(0.05)

    try:
        yield acquired
    finally:
        if acquired:
            try:
                # Only release the lock if we still own it.
                if client.get(key) == token:
                    client.delete(key)
            except Exception:  # noqa: BLE001 - TTL will clean up
                logger.warning("failed to release redis lock %s", key)


def reset_locks() -> None:
    """Test helper: drop cached Redis client and in-process locks."""
    global _redis_client
    _redis_client = None
    with _registry._guard:  # noqa: SLF001 - intentional test hook
        _registry._locks.clear()

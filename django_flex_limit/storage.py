"""Storage contracts and a process-local fixed-window backend."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from threading import RLock
from typing import Literal, Protocol

from .exceptions import RedisStorageError


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StorageDecision:
    """Atomic result of attempting to consume one slot."""

    allowed: bool
    remaining: int


class FixedWindowStorage(Protocol):
    """Backend contract used by :class:`FixedWindowRateLimiter`.

    Implementations must atomically test capacity and consume one request.
    Entries may be discarded at ``reset_at``. Keys are unique to a policy and
    aligned window.
    """

    def consume(self, key: str, *, limit: int, reset_at: float, now: float) -> StorageDecision:
        """Consume one slot, returning whether it was allowed and capacity left."""


class MemoryFixedWindowStorage:
    """Thread-safe in-process storage; counters are not shared across workers."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, tuple[int, float]] = {}

    def consume(self, key: str, *, limit: int, reset_at: float, now: float) -> StorageDecision:
        with self._lock:
            count, stored_reset = self._entries.get(key, (0, reset_at))
            if now >= stored_reset:
                count, stored_reset = 0, reset_at

            if count >= limit:
                self._entries[key] = (count, stored_reset)
                return StorageDecision(False, 0)

            count += 1
            self._entries[key] = (count, stored_reset)
            return StorageDecision(True, limit - count)


class RedisFixedWindowStorage:
    """Atomic fixed-window storage backed by Redis.

    Requires the optional ``redis`` package unless a compatible client is
    injected. The key is expired at the absolute window boundary supplied by
    the engine. ``fail_policy`` controls behavior when Redis raises:

    * ``"open"`` logs the failure and treats this request as allowed.
    * ``"closed"`` logs the failure and treats this request as denied.
    * ``"raise"`` raises :class:`RedisStorageError` with the client error as
      its cause.

    Open/closed modes log every Redis error so the fallback cannot be
    mistaken for a successful Redis operation.
    """

    _CONSUME_SCRIPT = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then
        redis.call('PEXPIREAT', KEYS[1], ARGV[1])
    end
    return count
    """

    def __init__(
        self,
        client: object | None = None,
        *,
        url: str | None = None,
        key_prefix: str = "django-flex-limit:",
        fail_policy: Literal["open", "closed", "raise"] = "closed",
    ) -> None:
        if fail_policy not in {"open", "closed", "raise"}:
            raise ValueError("fail_policy must be 'open', 'closed', or 'raise'")
        if not isinstance(key_prefix, str):
            raise TypeError("key_prefix must be a string")
        if client is not None and url is not None:
            raise ValueError("pass either client or url, not both")
        if client is None:
            if url is None:
                raise ValueError("a Redis client or url is required")
            try:
                import redis
            except ImportError as exc:
                raise RedisStorageError(
                    "Redis storage requires the optional 'redis' package"
                ) from exc
            client = redis.Redis.from_url(url)
        if not callable(getattr(client, "eval", None)):
            raise TypeError("client must provide a callable eval() method")
        self._client = client
        self._key_prefix = key_prefix
        self._fail_policy = fail_policy

    def consume(self, key: str, *, limit: int, reset_at: float, now: float) -> StorageDecision:
        redis_key = f"{self._key_prefix}{key}"
        expire_at_ms = math.ceil(reset_at * 1000)
        try:
            count = int(self._client.eval(self._CONSUME_SCRIPT, 1, redis_key, expire_at_ms))
        except Exception as exc:
            return self._handle_error(exc, limit)
        if count < 1:
            error = ValueError(f"Redis returned invalid counter value {count}")
            return self._handle_error(error, limit)
        return StorageDecision(allowed=count <= limit, remaining=max(0, limit - count))

    def _handle_error(self, error: Exception, limit: int) -> StorageDecision:
        message = "Redis rate-limit storage operation failed"
        if self._fail_policy == "raise":
            raise RedisStorageError(message) from error
        if self._fail_policy == "open":
            logger.exception("%s; fail-open policy allows the request", message, exc_info=error)
            return StorageDecision(allowed=True, remaining=max(0, limit - 1))
        logger.exception("%s; fail-closed policy denies the request", message, exc_info=error)
        return StorageDecision(allowed=False, remaining=0)

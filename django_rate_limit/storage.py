"""Storage contracts and rate-limit storage backends."""

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


@dataclass(frozen=True, slots=True)
class TokenBucketDecision:
    """Atomic result of attempting to consume one token.

    ``reset_at`` is the next time a request can be admitted.  It is equal to
    ``now`` when another token is already available.
    """

    allowed: bool
    remaining: int
    reset_at: float


class FixedWindowStorage(Protocol):
    """Backend contract used by :class:`FixedWindowRateLimiter`.

    Implementations must atomically test capacity and consume one request.
    Entries may be discarded at ``reset_at``. Keys are unique to a policy and
    aligned window.
    """

    def consume(self, key: str, *, limit: int, reset_at: float, now: float) -> StorageDecision:
        """Consume one slot, returning whether it was allowed and capacity left."""


class TokenBucketStorage(Protocol):
    """Backend contract for an atomic token bucket."""

    def consume(
        self,
        key: str,
        *,
        capacity: int,
        refill_rate: float,
        now: float,
    ) -> TokenBucketDecision:
        """Refill and consume one token, returning the resulting decision."""


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


class MemoryTokenBucketStorage:
    """Thread-safe in-process token bucket storage.

    State is process-local and therefore is not shared between Django worker
    processes.  The complete refill and consume operation is protected by one
    lock, so concurrent requests cannot overspend a bucket.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, tuple[float, float]] = {}

    def consume(
        self,
        key: str,
        *,
        capacity: int,
        refill_rate: float,
        now: float,
    ) -> TokenBucketDecision:
        with self._lock:
            tokens, last_refill = self._entries.get(key, (float(capacity), now))
            effective_now = max(now, last_refill)
            tokens = min(capacity, tokens + (effective_now - last_refill) * refill_rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._entries[key] = (tokens, effective_now)
            remaining = max(0, min(capacity, math.floor(tokens + 1e-12)))
            reset_at = effective_now if remaining >= 1 else effective_now + (1.0 - tokens) / refill_rate
            return TokenBucketDecision(allowed, remaining, reset_at)


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
        key_prefix: str = "django-rate-limit:",
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


class RedisTokenBucketStorage:
    """Atomic token bucket storage backed by Redis.

    The refill and consume operation runs in one Lua script.  Bucket state is
    kept in a Redis hash and expires after enough time for an idle bucket to
    refill completely, preventing unbounded key growth.
    """

    _CONSUME_SCRIPT = """
    local now = tonumber(ARGV[1])
    local capacity = tonumber(ARGV[2])
    local refill = tonumber(ARGV[3])
    local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
    local last = tonumber(redis.call('HGET', KEYS[1], 'last'))
    if not tokens or not last then
        tokens = capacity
        last = now
    end
    if now > last then
        tokens = math.min(capacity, tokens + (now - last) * refill)
        last = now
    end
    local allowed = 0
    if tokens >= 1 then
        tokens = tokens - 1
        allowed = 1
    end
    local remaining = math.floor(tokens)
    local reset = now
    if remaining < 1 then
        reset = now + (1 - tokens) / refill
    end
    redis.call('HSET', KEYS[1], 'tokens', tokens, 'last', last)
    redis.call('EXPIRE', KEYS[1], math.ceil(capacity / refill) + 1)
    return {allowed, remaining, reset}
    """

    def __init__(
        self,
        client: object | None = None,
        *,
        url: str | None = None,
        key_prefix: str = "django-rate-limit:",
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

    def consume(
        self,
        key: str,
        *,
        capacity: int,
        refill_rate: float,
        now: float,
    ) -> TokenBucketDecision:
        redis_key = f"{self._key_prefix}{key}"
        try:
            raw = self._client.eval(
                self._CONSUME_SCRIPT,
                1,
                redis_key,
                now,
                capacity,
                refill_rate,
            )
            allowed, remaining, reset_at = (int(raw[0]), int(raw[1]), float(raw[2]))
            if allowed not in (0, 1) or remaining < 0 or remaining > capacity:
                raise ValueError("Redis returned an invalid token bucket decision")
            return TokenBucketDecision(bool(allowed), remaining, reset_at)
        except Exception as exc:
            return self._handle_token_error(exc, capacity, now)

    def _handle_token_error(self, error: Exception, capacity: int, now: float) -> TokenBucketDecision:
        message = "Redis token-bucket storage operation failed"
        if self._fail_policy == "raise":
            raise RedisStorageError(message) from error
        if self._fail_policy == "open":
            logger.exception("%s; fail-open policy allows the request", message, exc_info=error)
            return TokenBucketDecision(True, max(0, capacity - 1), now)
        logger.exception("%s; fail-closed policy denies the request", message, exc_info=error)
        return TokenBucketDecision(False, 0, now)

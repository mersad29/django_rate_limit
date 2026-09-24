"""Fixed-window rate-limit policy and evaluator, independent of Django."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Callable

from .exceptions import InvalidRateLimit, StorageError
from .result import RateLimitResult
from .storage import FixedWindowStorage, TokenBucketStorage


_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*([a-zA-Z]+)\s*$")
_PERIODS = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


@dataclass(frozen=True, slots=True)
class RateLimit:
    """A maximum number of requests in a period measured in seconds."""

    limit: int
    period: int

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise InvalidRateLimit("limit must be a positive integer")
        if isinstance(self.period, bool) or not isinstance(self.period, int) or self.period <= 0:
            raise InvalidRateLimit("period must be a positive integer number of seconds")


def parse_rate(value: str) -> RateLimit:
    """Parse strings such as ``'5/m'``, ``'100/h'`` or ``'2/seconds'``."""
    if not isinstance(value, str):
        raise InvalidRateLimit("rate must be a string in '<count>/<period>' format")
    match = _RATE_RE.fullmatch(value)
    if match is None:
        raise InvalidRateLimit(f"invalid rate {value!r}; expected '<positive integer>/<period>'")
    count = int(match.group(1))
    unit = match.group(2).lower()
    if count <= 0:
        raise InvalidRateLimit("rate count must be a positive integer")
    try:
        period = _PERIODS[unit]
    except KeyError as exc:
        supported = ", ".join(("s", "m", "h", "d"))
        raise InvalidRateLimit(f"unsupported period {unit!r}; use one of {supported}") from exc
    return RateLimit(count, period)


class FixedWindowRateLimiter:
    """Evaluate fixed-window limits using an injected atomic storage backend.

    Windows align to Unix epoch boundaries. ``key`` should identify the caller
    and policy (for example ``'checkout:user-42'``); the engine adds the window
    number so unrelated windows never share a counter.
    """

    def __init__(
        self,
        storage: FixedWindowStorage,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if storage is None or not callable(getattr(storage, "consume", None)):
            raise TypeError("storage must provide a callable consume() method")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._storage = storage
        self._clock = clock

    def check(self, rate: str | RateLimit, *, key: str) -> RateLimitResult:
        """Consume one request for ``key`` and return its decision."""
        policy = parse_rate(rate) if isinstance(rate, str) else rate
        if not isinstance(policy, RateLimit):
            raise InvalidRateLimit("rate must be a rate string or RateLimit instance")
        if not isinstance(key, str) or not key.strip():
            raise InvalidRateLimit("key must be a non-empty string")

        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise StorageError("clock must return a finite Unix timestamp")
        now = float(now)
        window = math.floor(now / policy.period)
        reset_at = (window + 1) * policy.period
        bucket_key = f"{key}:{policy.limit}:{policy.period}:{window}"
        try:
            decision = self._storage.consume(
                bucket_key, limit=policy.limit, reset_at=reset_at, now=now
            )
        except Exception as exc:
            raise StorageError("storage failed to consume rate-limit capacity") from exc
        if not isinstance(decision.allowed, bool) or not isinstance(decision.remaining, int):
            raise StorageError("storage returned an invalid decision")
        if not 0 <= decision.remaining <= policy.limit:
            raise StorageError("storage returned remaining capacity outside the policy bounds")
        return RateLimitResult(
            allowed=decision.allowed,
            limit=policy.limit,
            remaining=decision.remaining,
            reset_at=reset_at,
            retry_after=max(0.0, reset_at - now) if not decision.allowed else 0.0,
        )


class TokenBucketRateLimiter:
    """Evaluate a token-bucket policy using an injected atomic storage backend.

    The rate's count is the bucket capacity and its period determines the
    refill rate.  For example, ``"5/m"`` starts with five tokens and refills
    at five tokens per minute.  A request consumes one token.
    """

    def __init__(
        self,
        storage: TokenBucketStorage,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if storage is None or not callable(getattr(storage, "consume", None)):
            raise TypeError("storage must provide a callable consume() method")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._storage = storage
        self._clock = clock

    def check(self, rate: str | RateLimit, *, key: str) -> RateLimitResult:
        """Consume one token for ``key`` and return its decision."""
        policy = parse_rate(rate) if isinstance(rate, str) else rate
        if not isinstance(policy, RateLimit):
            raise InvalidRateLimit("rate must be a rate string or RateLimit instance")
        if not isinstance(key, str) or not key.strip():
            raise InvalidRateLimit("key must be a non-empty string")

        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise StorageError("clock must return a finite Unix timestamp")
        now = float(now)
        refill_rate = policy.limit / policy.period
        bucket_key = f"{key}:{policy.limit}:{policy.period}"
        try:
            decision = self._storage.consume(
                bucket_key,
                capacity=policy.limit,
                refill_rate=refill_rate,
                now=now,
            )
        except Exception as exc:
            raise StorageError("storage failed to consume token-bucket capacity") from exc
        if not isinstance(decision.allowed, bool) or not isinstance(decision.remaining, int):
            raise StorageError("storage returned an invalid token-bucket decision")
        if not 0 <= decision.remaining <= policy.limit:
            raise StorageError("storage returned remaining capacity outside the policy bounds")
        if (
            isinstance(decision.reset_at, bool)
            or not isinstance(decision.reset_at, (int, float))
            or not math.isfinite(decision.reset_at)
        ):
            raise StorageError("storage returned an invalid token-bucket reset time")
        reset_at = float(decision.reset_at)
        retry_after = max(0.0, reset_at - now) if not decision.allowed else 0.0
        return RateLimitResult(
            allowed=decision.allowed,
            limit=policy.limit,
            remaining=decision.remaining,
            reset_at=reset_at,
            retry_after=retry_after,
        )

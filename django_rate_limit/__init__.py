"""Framework-neutral core for the django-rate-limit package."""

from .engine import FixedWindowRateLimiter, RateLimit, TokenBucketRateLimiter, parse_rate
from .decorators import rate_limit
from .exceptions import InvalidRateLimit, RateLimitError, RedisStorageError, StorageError
from .result import RateLimitResult
from .storage import (
    FixedWindowStorage,
    MemoryFixedWindowStorage,
    MemoryTokenBucketStorage,
    RedisFixedWindowStorage,
    RedisTokenBucketStorage,
    StorageDecision,
    TokenBucketDecision,
    TokenBucketStorage,
)

__all__ = [
    "FixedWindowRateLimiter",
    "FixedWindowStorage",
    "InvalidRateLimit",
    "MemoryFixedWindowStorage",
    "MemoryTokenBucketStorage",
    "RateLimit",
    "RateLimitError",
    "RateLimitResult",
    "RedisFixedWindowStorage",
    "RedisStorageError",
    "RedisTokenBucketStorage",
    "rate_limit",
    "StorageDecision",
    "StorageError",
    "TokenBucketDecision",
    "TokenBucketRateLimiter",
    "TokenBucketStorage",
    "parse_rate",
]

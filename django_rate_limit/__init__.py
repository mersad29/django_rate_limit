"""Framework-neutral core for the django-rate-limit package."""

from .engine import FixedWindowRateLimiter, RateLimit, parse_rate
from .decorators import rate_limit
from .exceptions import InvalidRateLimit, RateLimitError, RedisStorageError, StorageError
from .result import RateLimitResult
from .storage import (
    FixedWindowStorage,
    MemoryFixedWindowStorage,
    RedisFixedWindowStorage,
    StorageDecision,
)

__all__ = [
    "FixedWindowRateLimiter",
    "FixedWindowStorage",
    "InvalidRateLimit",
    "MemoryFixedWindowStorage",
    "RateLimit",
    "RateLimitError",
    "RateLimitResult",
    "RedisFixedWindowStorage",
    "RedisStorageError",
    "rate_limit",
    "StorageDecision",
    "StorageError",
    "parse_rate",
]

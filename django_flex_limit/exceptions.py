"""Exceptions raised by django-flex-limit's framework-neutral core."""


class RateLimitError(Exception):
    """Base class for rate-limit core errors."""


class InvalidRateLimit(RateLimitError, ValueError):
    """A rate-limit value or policy is invalid."""


class StorageError(RateLimitError):
    """A storage backend could not perform the requested operation."""


class RedisStorageError(StorageError):
    """Redis could not perform a rate-limit operation."""

"""Framework-neutral outcomes returned by the rate-limit engine."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome for one request in a fixed window.

    ``reset_at`` and ``retry_after`` are Unix timestamps/seconds in UTC time.
    ``retry_after`` is zero for allowed requests and positive for denied ones.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_at: float
    retry_after: float


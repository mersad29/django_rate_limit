from concurrent.futures import ThreadPoolExecutor

import pytest

from django_rate_limit import (
    FixedWindowRateLimiter,
    InvalidRateLimit,
    MemoryFixedWindowStorage,
    RateLimit,
    StorageError,
    parse_rate,
)


@pytest.mark.parametrize(
    ("text", "limit", "period"),
    [("5/m", 5, 60), ("100/h", 100, 3600), ("2/seconds", 2, 1), (" 3 / days ", 3, 86400)],
)
def test_parse_rate(text, limit, period):
    assert parse_rate(text) == RateLimit(limit, period)


@pytest.mark.parametrize(
    "value",
    ["", "5", "/m", "0/m", "-1/m", "1.5/m", "1/ms", "1/week", "1/m/extra", "abc/h", None, 5],
)
def test_parse_rate_rejects_invalid_values(value):
    with pytest.raises(InvalidRateLimit):
        parse_rate(value)


@pytest.mark.parametrize("limit,period", [(0, 60), (-1, 60), (True, 60), (1, 0), (1, -5), (1, True)])
def test_policy_rejects_invalid_numbers(limit, period):
    with pytest.raises(InvalidRateLimit):
        RateLimit(limit, period)


def test_limit_is_allowed_exactly_limit_times_then_denied():
    limiter = FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: 10)
    results = [limiter.check("2/m", key="user:1") for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert [r.remaining for r in results] == [1, 0, 0]
    assert results[-1].retry_after == 50


def test_window_boundary_resets_counter_and_has_new_reset_time():
    now = [59.999]
    limiter = FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: now[0])
    assert limiter.check("1/m", key="k").allowed
    assert not limiter.check("1/m", key="k").allowed
    now[0] = 60
    result = limiter.check("1/m", key="k")
    assert result.allowed
    assert result.remaining == 0
    assert result.reset_at == 120


def test_negative_timestamp_uses_floor_for_aligned_window():
    limiter = FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: -0.5)
    result = limiter.check("1/m", key="k")
    assert result.reset_at == 0


def test_keys_are_isolated():
    limiter = FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: 10)
    assert limiter.check("1/m", key="a").allowed
    assert limiter.check("1/m", key="b").allowed


def test_concurrent_memory_consumes_never_exceed_limit():
    limiter = FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: 10)
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda _: limiter.check("10/m", key="shared"), range(100)))
    assert sum(result.allowed for result in results) == 10
    assert all(result.remaining >= 0 for result in results)


def test_storage_is_injected_and_receives_aligned_window_details():
    class RecordingStorage:
        def __init__(self):
            self.args = None

        def consume(self, key, *, limit, reset_at, now):
            self.args = (key, limit, reset_at, now)
            from django_rate_limit import StorageDecision
            return StorageDecision(True, limit - 1)

    storage = RecordingStorage()
    result = FixedWindowRateLimiter(storage, clock=lambda: 121).check("5/m", key="scope")
    assert storage.args == ("scope:5:60:2", 5, 180, 121.0)
    assert result.reset_at == 180


def test_storage_errors_are_wrapped_with_cause():
    class BrokenStorage:
        def consume(self, *args, **kwargs):
            raise OSError("offline")

    with pytest.raises(StorageError) as exc:
        FixedWindowRateLimiter(BrokenStorage(), clock=lambda: 1).check("1/m", key="k")
    assert isinstance(exc.value.__cause__, OSError)


@pytest.mark.parametrize("key", ["", "  ", None, 4])
def test_empty_or_non_string_key_is_rejected(key):
    limiter = FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: 1)
    with pytest.raises(InvalidRateLimit, match="key"):
        limiter.check("1/m", key=key)


def test_non_rate_policy_and_bad_clock_are_rejected():
    limiter = FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: float("nan"))
    with pytest.raises(StorageError, match="clock"):
        limiter.check("1/m", key="k")
    with pytest.raises(InvalidRateLimit, match="RateLimit"):
        FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: 1).check(123, key="k")


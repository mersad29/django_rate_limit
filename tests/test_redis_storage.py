from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import pytest

from django_rate_limit import RedisFixedWindowStorage, RedisStorageError


class FakeRedis:
    """Small eval-compatible test double for Redis' atomic Lua execution."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def eval(self, script, numkeys, key, expire_at_ms):
        assert numkeys == 1
        with self.lock:
            now_ms = time.time() * 1000
            entry = self.entries.get(key)
            if entry is not None and entry[1] is not None and entry[1] <= now_ms:
                del self.entries[key]
                entry = None
            count = (entry[0] if entry else 0) + 1
            expiry = entry[1] if entry else int(expire_at_ms)
            self.entries[key] = (count, expiry)
            return count


def test_redis_storage_uses_atomic_consume_and_returns_remaining():
    storage = RedisFixedWindowStorage(FakeRedis(), key_prefix="unit:")
    expiry = time.time() + 100
    assert storage.consume("caller:1", limit=2, reset_at=expiry, now=time.time()).allowed
    second = storage.consume("caller:1", limit=2, reset_at=expiry, now=time.time())
    assert second.allowed and second.remaining == 0
    third = storage.consume("caller:1", limit=2, reset_at=expiry, now=time.time())
    assert not third.allowed and third.remaining == 0


def test_redis_storage_passes_absolute_expiration_to_script():
    client = FakeRedis()
    storage = RedisFixedWindowStorage(client)
    storage.consume("expiring", limit=1, reset_at=123.4567, now=100)
    stored_expiry = next(iter(client.entries.values()))[1]
    assert stored_expiry == 123457


def test_redis_storage_fake_concurrent_consumes_respect_limit():
    storage = RedisFixedWindowStorage(FakeRedis())
    expiry = time.time() + 100
    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(
            lambda _: storage.consume("same", limit=7, reset_at=expiry, now=time.time()), range(100)
        ))
    assert sum(decision.allowed for decision in decisions) == 7


@pytest.mark.parametrize("policy,allowed", [("open", True), ("closed", False)])
def test_backend_failure_policy_is_logged_and_applied(policy, allowed, caplog):
    class BrokenRedis:
        def eval(self, *args):
            raise ConnectionError("redis offline")

    storage = RedisFixedWindowStorage(BrokenRedis(), fail_policy=policy)
    decision = storage.consume("k", limit=5, reset_at=100, now=10)
    assert decision.allowed is allowed
    assert "Redis rate-limit storage operation failed" in caplog.text
    assert "fail-open" in caplog.text if allowed else "fail-closed" in caplog.text


def test_backend_failure_raise_policy_uses_explicit_library_exception():
    class BrokenRedis:
        def eval(self, *args):
            raise ConnectionError("redis offline")

    storage = RedisFixedWindowStorage(BrokenRedis(), fail_policy="raise")
    with pytest.raises(RedisStorageError) as raised:
        storage.consume("k", limit=1, reset_at=100, now=10)
    assert isinstance(raised.value.__cause__, ConnectionError)


@pytest.mark.parametrize("policy", ["ignore", "fail-open", None])
def test_unknown_failure_policy_is_rejected(policy):
    with pytest.raises(ValueError, match="fail_policy"):
        RedisFixedWindowStorage(FakeRedis(), fail_policy=policy)


def _live_storage():
    redis = pytest.importorskip("redis", reason="install django-rate-limit[redis]")
    url = __import__("os").environ.get("DJANGO_RATE_LIMIT_REDIS_URL")
    if not url:
        pytest.skip("set DJANGO_RATE_LIMIT_REDIS_URL to run Redis integration tests")
    prefix = f"django-rate-limit-test:{uuid.uuid4().hex}:"
    client = redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=1)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"configured Redis is unavailable: {exc}")
    return client, RedisFixedWindowStorage(client, key_prefix=prefix)


def test_live_redis_expires_fixed_window_counter():
    client, storage = _live_storage()
    try:
        key = uuid.uuid4().hex
        expiry = time.time() + 0.2
        assert storage.consume(key, limit=1, reset_at=expiry, now=time.time()).allowed
        assert not storage.consume(key, limit=1, reset_at=expiry, now=time.time()).allowed
        time.sleep(0.3)
        assert storage.consume(key, limit=1, reset_at=time.time() + 1, now=time.time()).allowed
    finally:
        client.close()


def test_live_redis_concurrent_requests_never_exceed_limit():
    client, storage = _live_storage()
    try:
        key = uuid.uuid4().hex
        expiry = time.time() + 60
        with ThreadPoolExecutor(max_workers=24) as pool:
            decisions = list(pool.map(
                lambda _: storage.consume(key, limit=13, reset_at=expiry, now=time.time()),
                range(120),
            ))
        assert sum(decision.allowed for decision in decisions) == 13
    finally:
        client.close()


def test_live_redis_connection_failure_raises_library_exception():
    redis = pytest.importorskip("redis", reason="install django-rate-limit[redis]")
    client = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.1)
    storage = RedisFixedWindowStorage(client, fail_policy="raise")
    try:
        with pytest.raises(RedisStorageError):
            storage.consume("offline", limit=1, reset_at=time.time() + 30, now=time.time())
    finally:
        client.close()

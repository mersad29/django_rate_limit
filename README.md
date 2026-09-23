# django-rate-limit

`django-rate-limit` is a small, reusable rate-limiting library for Django projects.
It keeps the rate-limit engine independent from Django HTTP responses and lets the
Django integration live at the decorator layer.

The current release includes:

- Function-based view support through `@rate_limit`
- Class-based view support when decorating `dispatch` with Django's `method_decorator`
- Fixed Window rate limiting
- In-process memory storage
- Redis storage with atomic counter updates
- HTTP 429 responses for blocked Django requests
- A framework-neutral core that can be tested without Django

Sliding Window Counter support is planned for a later release.

## Installation

Install from source:

```bash
pip install .
```

Install with Redis support:

```bash
pip install ".[redis]"
```

## Quick Start

Use the decorator on a function-based view:

```python
from django.http import HttpResponse
from django_rate_limit import rate_limit


@rate_limit("5/m")
def checkout(request):
    return HttpResponse("ok")
```

When the limit is exceeded, the decorator returns:

- status code `429`
- plain text body `Too Many Requests`
- `Retry-After` header in seconds

## Class-Based Views

Decorate `dispatch` with Django's `method_decorator`:

```python
from django.utils.decorators import method_decorator
from django.views import View
from django.http import HttpResponse
from django_rate_limit import rate_limit


@method_decorator(rate_limit("20/h"), name="dispatch")
class DashboardView(View):
    def get(self, request):
        return HttpResponse("dashboard")
```

## Decorator Options

```python
@rate_limit(
    "10/m",
    key="ip",
    methods=["POST"],
    backend=None,
    algorithm=None,
    block=True,
)
def view(request):
    ...
```

### `rate`

Rate strings use `<count>/<period>`:

```python
"5/s"
"100/m"
"1000/h"
"5000/d"
```

Supported period aliases include seconds, minutes, hours, and days:

```python
"5/sec"
"5/second"
"5/seconds"
"20/minute"
"100/hour"
"1000/day"
```

Invalid rates raise `InvalidRateLimit`.

### `key`

The default key is the client IP address from `request.META["REMOTE_ADDR"]`.

```python
@rate_limit("10/m", key="ip")
```

Use authenticated Django users when available, falling back to IP for anonymous
requests:

```python
@rate_limit("10/m", key="user")
```

Or provide a custom callable:

```python
@rate_limit("10/m", key=lambda request: request.headers.get("X-Api-Key", "anonymous"))
```

The decorator combines this identity with the decorated view name, so separate
views do not share counters by default.

### `methods`

Limit only selected HTTP methods:

```python
@rate_limit("5/m", methods=["POST", "PUT"])
```

Requests using other methods continue through the normal Django lifecycle.

### `block`

By default, denied requests return HTTP 429:

```python
@rate_limit("5/m", block=True)
```

Set `block=False` to record and evaluate the request but allow the view to run:

```python
@rate_limit("5/m", block=False)
```

This is useful when observing a new policy before enforcing it.

## Redis Storage

Redis storage is injected into the decorator through `backend`.

```python
from django_rate_limit import RedisFixedWindowStorage, rate_limit


redis_backend = RedisFixedWindowStorage(
    url="redis://localhost:6379/0",
    fail_policy="closed",
)


@rate_limit("100/m", backend=redis_backend)
def api_view(request):
    ...
```

You can also inject a compatible Redis client:

```python
RedisFixedWindowStorage(client=redis_client)
```

Redis failures are explicit. The `fail_policy` option controls how requests are
handled when Redis raises:

- `"closed"` denies the request
- `"open"` allows the request
- `"raise"` raises `RedisStorageError`

Open and closed modes log every Redis error.

## Framework-Neutral Core

The core engine does not know about Django requests or responses:

```python
from django_rate_limit import FixedWindowRateLimiter, MemoryFixedWindowStorage


storage = MemoryFixedWindowStorage()
limiter = FixedWindowRateLimiter(storage)

result = limiter.check("5/m", key="user:42")

if result.allowed:
    print(result.remaining)
else:
    print(result.retry_after)
```

`RateLimitResult` contains:

- `allowed`
- `limit`
- `remaining`
- `reset_at`
- `retry_after`

Storage is injected through the `FixedWindowStorage` protocol. A storage backend
must atomically consume one slot and return a `StorageDecision`.

## Concurrency Notes

`MemoryFixedWindowStorage` is protected by a process-local lock. It is suitable
for development, tests, and single-process deployments, but counters are not
shared across multiple workers or servers.

`RedisFixedWindowStorage` uses a Lua script so increment and expiration setup
happen atomically in Redis. Use Redis storage when limits must be shared across
processes, workers, or hosts.

Fixed Window limits are simple and fast, but bursts can happen around window
boundaries. For example, a client may use the full limit at the end of one
minute and again at the start of the next. Sliding Window Counter support is the
planned next algorithm for smoother enforcement.

## Development

Run the test suite:

```bash
pytest -q
```

The Redis tests include fake-client coverage by default. Live Redis integration
tests run only when both the optional Redis package and a Redis URL are
available:

```bash
pip install ".[redis]"
set DJANGO_RATE_LIMIT_REDIS_URL=redis://localhost:6379/0
pytest -q
```

On PowerShell:

```powershell
$env:DJANGO_RATE_LIMIT_REDIS_URL = "redis://localhost:6379/0"
pytest -q
```

## Public API

```python
from django_rate_limit import (
    FixedWindowRateLimiter,
    FixedWindowStorage,
    InvalidRateLimit,
    MemoryFixedWindowStorage,
    RateLimit,
    RateLimitError,
    RateLimitResult,
    RedisFixedWindowStorage,
    RedisStorageError,
    StorageDecision,
    StorageError,
    parse_rate,
    rate_limit,
)
```

## License

No license file is included yet. Add one before publishing the package for
external reuse.

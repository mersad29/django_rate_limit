# django-rate-limit

`django-rate-limit` is a small, reusable rate-limiting library for Django projects.
It keeps the rate-limit engine independent from Django HTTP responses and lets the
Django integration live at the decorator layer.

The current release includes:

- Function-based view support through `@rate_limit`
- Class-based view support when decorating `dispatch` with Django's `method_decorator`
- Fixed Window rate limiting
- In-process memory storage
- HTTP 429 responses for blocked Django requests
- A framework-neutral core that can be tested without Django

Sliding Window Counter, Token Bucket and Redis support is planned for a later release.

## Installation

Install from source:

```bash
pip install git+https://github.com/mersad29/django_rate_limit.git
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
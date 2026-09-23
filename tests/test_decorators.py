from functools import wraps

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils.decorators import method_decorator

from django_rate_limit import MemoryFixedWindowStorage
from django_rate_limit.decorators import rate_limit


if not settings.configured:
    settings.configure(DEFAULT_CHARSET="utf-8", SECRET_KEY="test-only")


@pytest.fixture
def rf():
    return RequestFactory()


def test_function_view_returns_429_then_allows_new_window(rf):
    now = [10.0]
    from django_rate_limit import FixedWindowRateLimiter

    limiter = FixedWindowRateLimiter(MemoryFixedWindowStorage(), clock=lambda: now[0])

    @rate_limit("1/m", key=lambda request: request.META["REMOTE_ADDR"], algorithm=limiter)
    def view(request):
        return HttpResponse("ok")

    request = rf.get("/", REMOTE_ADDR="192.0.2.1")
    assert view(request).status_code == 200
    denied = view(request)
    assert denied.status_code == 429
    assert denied["Retry-After"] == "50"
    now[0] = 60
    assert view(request).status_code == 200


def test_metadata_is_preserved(rf):
    def mark(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            return view(*args, **kwargs)
        wrapper.custom_marker = "kept"
        return wrapper

    @rate_limit("2/m")
    @mark
    def sample_view(request):
        """Original docs."""
        return HttpResponse("ok")

    assert sample_view.__name__ == "sample_view"
    assert sample_view.__doc__ == "Original docs."
    assert sample_view.custom_marker == "kept"
    assert sample_view.__wrapped__.__name__ == "sample_view"


def test_methods_only_limit_selected_methods(rf):
    @rate_limit("1/m", methods=["POST"])
    def view(request):
        return HttpResponse("ok")

    request_get = rf.get("/")
    assert view(request_get).status_code == 200
    assert view(request_get).status_code == 200
    request_post = rf.post("/")
    assert view(request_post).status_code == 200
    assert view(request_post).status_code == 429


def test_non_blocking_mode_records_but_continues(rf):
    @rate_limit("1/m", block=False)
    def view(request):
        return HttpResponse("still served")

    request = rf.get("/")
    assert view(request).content == b"still served"
    assert view(request).status_code == 200


def test_custom_backend_factory_and_algorithm_factory_are_injected(rf):
    events = []

    class Backend:
        def consume(self, *args, **kwargs):
            events.append("consume")

    class Limiter:
        def __init__(self, backend):
            assert isinstance(backend, Backend)

        def check(self, rate, *, key):
            events.append((rate, key))
            from django_rate_limit import RateLimitResult
            return RateLimitResult(True, 1, 0, 60, 0)

    @rate_limit("1/m", backend=Backend, algorithm=Limiter)
    def view(request):
        return HttpResponse("ok")

    assert view(rf.get("/")).status_code == 200
    assert events[0][0] == "1/m"
    assert ".view:" in events[0][1]


def test_explicit_user_key_uses_authenticated_user_or_ip_fallback(rf):
    @rate_limit("1/m", key="user")
    def view(request):
        return HttpResponse("ok")

    request = rf.get("/", REMOTE_ADDR="198.51.100.3")
    request.user = type("User", (), {"is_authenticated": True, "pk": 17})()
    assert view(request).status_code == 200
    assert view(request).status_code == 429


def test_cbv_dispatch_method_decorator(rf):
    class View:
        @method_decorator(rate_limit("1/m"))
        def dispatch(self, request, *args, **kwargs):
            return HttpResponse("ok")

    view = View()
    request = rf.get("/")
    assert view.dispatch(request).status_code == 200
    assert view.dispatch(request).status_code == 429


@pytest.mark.parametrize("kwargs", [{"methods": []}, {"methods": [""]}, {"key": "header"}, {"block": 1}])
def test_invalid_decorator_configuration(kwargs):
    with pytest.raises((TypeError, ValueError)):
        rate_limit("1/m", **kwargs)


def test_async_function_view_preserves_async_response_lifecycle(rf):
    import asyncio

    @rate_limit("1/m")
    async def view(request):
        await asyncio.sleep(0)
        return HttpResponse("async ok")

    response = asyncio.run(view(rf.get("/")))
    assert response.status_code == 200
    assert response.content == b"async ok"

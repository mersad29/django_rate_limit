"""Django view decorators for the framework-neutral rate-limit core."""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable, Iterable

from django.http import HttpRequest, HttpResponse

from .engine import FixedWindowRateLimiter, TokenBucketRateLimiter
from .storage import MemoryFixedWindowStorage, MemoryTokenBucketStorage


# A shared process-local store allows separate decorated views to work without
# settings, while view-scoped keys keep their counters independent.
_DEFAULT_BACKEND = MemoryFixedWindowStorage()
_DEFAULT_TOKEN_BUCKET_BACKEND = MemoryTokenBucketStorage()


def rate_limit(
    rate: str,
    *,
    key: str | Callable[[HttpRequest], object] | None = None,
    methods: Iterable[str] | None = None,
    algorithm: Any = None,
    backend: Any = None,
    block: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Limit a Django view and return HTTP 429 when its policy is exceeded.

    ``key`` may be ``"ip"`` (the default), ``"user"``, or a callable that
    receives the request and returns a caller identifier. Caller identifiers
    are always combined with the decorated view's module and qualified name.

    ``backend`` is a storage instance implementing ``consume`` or a zero-arg
    factory that returns one. ``algorithm`` is a limiter instance implementing
    ``check(rate, key=...)`` or a factory/class accepting the backend as its
    sole positional argument. The default is the existing
    :class:`FixedWindowRateLimiter` with process-local memory storage.

    ``methods`` restricts limiting to the listed HTTP methods. ``block=False``
    still records/evaluates the request, but lets it continue after denial.
    The decorator supports sync and async function views and can be applied to
    a CBV's ``dispatch`` using Django's ``method_decorator``.
    """
    if not isinstance(rate, str):
        raise TypeError("rate must be a rate string")
    if not isinstance(block, bool):
        raise TypeError("block must be a bool")
    allowed_methods = None
    if methods is not None:
        try:
            allowed_methods = frozenset(_validate_method(method) for method in methods)
        except TypeError as exc:
            raise TypeError("methods must be an iterable of HTTP method names") from exc
        if not allowed_methods:
            raise ValueError("methods must contain at least one HTTP method")
    if key is not None and not callable(key) and key not in {"ip", "user"}:
        raise ValueError("key must be 'ip', 'user', or a request-to-key callable")

    store = _resolve_backend(backend, algorithm)
    limiter = _resolve_algorithm(algorithm, store)
    if not callable(getattr(limiter, "check", None)):
        raise TypeError("algorithm must provide a callable check(rate, key=...) method")

    def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
        view_scope = f"{view.__module__}.{view.__qualname__}"

        def evaluate(request: HttpRequest) -> HttpResponse | None:
            if allowed_methods is not None and request.method.upper() not in allowed_methods:
                return None
            identity = _request_key(request, key)
            result = limiter.check(rate, key=f"{view_scope}:{identity}")
            if result.allowed or not block:
                return None
            response = HttpResponse("Too Many Requests", status=429, content_type="text/plain")
            response["Retry-After"] = str(max(0, int(result.retry_after + 0.999999)))
            return response

        if inspect.iscoroutinefunction(view):
            @wraps(view)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                request = _find_request(args)
                denied = evaluate(request)
                if denied is not None:
                    return denied
                return await view(*args, **kwargs)

            return async_wrapper

        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            request = _find_request(args)
            denied = evaluate(request)
            if denied is not None:
                return denied
            return view(*args, **kwargs)

        return wrapper

    return decorate


def _validate_method(method: object) -> str:
    if not isinstance(method, str) or not method.strip():
        raise ValueError("HTTP methods must be non-empty strings")
    return method.upper()


def _resolve_backend(backend: Any, algorithm: Any = None) -> Any:
    if backend is None:
        if algorithm is TokenBucketRateLimiter:
            return _DEFAULT_TOKEN_BUCKET_BACKEND
        return _DEFAULT_BACKEND
    store = (
        backend()
        if callable(backend)
        and (inspect.isclass(backend) or not callable(getattr(backend, "consume", None)))
        else backend
    )
    if not callable(getattr(store, "consume", None)):
        raise TypeError("backend must provide consume() or be a factory returning such a backend")
    return store


def _resolve_algorithm(algorithm: Any, backend: Any) -> Any:
    if algorithm is None:
        return FixedWindowRateLimiter(backend)
    if not inspect.isclass(algorithm) and callable(getattr(algorithm, "check", None)):
        return algorithm
    if callable(algorithm):
        return algorithm(backend)
    raise TypeError("algorithm must provide check() or be a factory accepting the backend")


def _find_request(args: tuple[Any, ...]) -> HttpRequest:
    # A function view receives request first. A method_decorator applied to
    # dispatch receives (view_instance, request, ...).
    for arg in args[:2]:
        if hasattr(arg, "META") and hasattr(arg, "method"):
            return arg
    raise TypeError("rate_limit could not find a Django request in view arguments")


def _request_key(request: HttpRequest, key: Any) -> object:
    if callable(key):
        return key(request)
    if key == "user":
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return getattr(user, "pk", None) or getattr(user, "get_username", lambda: "")()
        return _client_ip(request)
    return _client_ip(request)


def _client_ip(request: HttpRequest) -> str:
    # REMOTE_ADDR is supplied by Django's server interface. Proxy headers are
    # intentionally not trusted implicitly; applications can pass a key fn.
    return request.META.get("REMOTE_ADDR") or "unknown"

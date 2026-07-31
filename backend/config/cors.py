from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_vary_headers


class CorsMiddleware:
    """Add CORS headers for explicitly configured browser origins."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        origin = request.headers.get("Origin", "")
        allowed = origin in set(settings.CORS_ALLOWED_ORIGINS)
        is_preflight = (
            request.method == "OPTIONS"
            and bool(request.headers.get("Access-Control-Request-Method"))
        )

        response = HttpResponse(status=204) if is_preflight else self.get_response(request)
        if origin:
            patch_vary_headers(response, ["Origin"])
        if not allowed:
            return response

        response["Access-Control-Allow-Origin"] = origin
        response["Access-Control-Allow-Methods"] = ", ".join(
            settings.CORS_ALLOWED_METHODS
        )
        response["Access-Control-Allow-Headers"] = ", ".join(
            settings.CORS_ALLOWED_HEADERS
        )
        response["Access-Control-Max-Age"] = str(settings.CORS_PREFLIGHT_MAX_AGE)
        return response

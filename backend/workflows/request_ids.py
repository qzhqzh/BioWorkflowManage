from __future__ import annotations

import re
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from rest_framework.response import Response


_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def request_id(request) -> str:
    cached = getattr(request, "_bioworkflow_request_id", "")
    if cached:
        return cached
    candidate = request.headers.get("X-Request-ID", "").strip()
    value = (
        candidate
        if _REQUEST_ID_PATTERN.fullmatch(candidate)
        else f"req_{uuid.uuid4().hex}"
    )
    request._bioworkflow_request_id = value
    return value


def with_request_id(response: Response, value: str) -> Response:
    response["X-Request-ID"] = value
    return response


class IntegrationRequestIdMiddleware:
    """Echo or create one stable request ID for every Integration API response."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not request.path.startswith("/api/v1/integration/"):
            return self.get_response(request)
        value = request_id(request)
        response = self.get_response(request)
        response["X-Request-ID"] = value
        return response

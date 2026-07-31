from __future__ import annotations

import re
import uuid

from rest_framework.response import Response


_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def request_id(request) -> str:
    candidate = request.headers.get("X-Request-ID", "").strip()
    if _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return f"req_{uuid.uuid4().hex}"


def with_request_id(response: Response, value: str) -> Response:
    response["X-Request-ID"] = value
    return response

from __future__ import annotations

import logging
from typing import Any

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .request_ids import request_id, with_request_id


logger = logging.getLogger(__name__)


ERROR_DEFAULTS: dict[int, tuple[str, str, str, bool]] = {
    status.HTTP_400_BAD_REQUEST: (
        "REQUEST_INVALID",
        "validation",
        "请求格式或参数无效。",
        False,
    ),
    status.HTTP_401_UNAUTHORIZED: (
        "SERVICE_AUTHENTICATION_REQUIRED",
        "authentication",
        "该接口需要有效的 Service Token 或管理员会话。",
        False,
    ),
    status.HTTP_403_FORBIDDEN: (
        "SERVICE_SCOPE_REQUIRED",
        "authorization",
        "当前身份缺少所需权限。",
        False,
    ),
    status.HTTP_404_NOT_FOUND: (
        "INTEGRATION_RESOURCE_NOT_FOUND",
        "validation",
        "请求的集成资源不存在。",
        False,
    ),
    status.HTTP_405_METHOD_NOT_ALLOWED: (
        "METHOD_NOT_ALLOWED",
        "validation",
        "该接口不支持当前 HTTP 方法。",
        False,
    ),
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: (
        "UNSUPPORTED_MEDIA_TYPE",
        "validation",
        "请求 Content-Type 不受支持。",
        False,
    ),
    status.HTTP_429_TOO_MANY_REQUESTS: (
        "RATE_LIMITED",
        "infrastructure",
        "请求过于频繁，请稍后重试。",
        True,
    ),
}


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def integration_exception_handler(exc, context):
    request = context.get("request")
    if request is None:
        return exception_handler(exc, context)

    response = exception_handler(exc, context)
    if request.path == "/api/v1/auth/login":
        if response is None or response.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
            return response
        value = request_id(request)
        response.data = {
            "error": {
                "code": "AUTH_RATE_LIMITED",
                "message": "登录尝试过于频繁，请稍后重试。",
                "request_id": value,
            }
        }
        return with_request_id(response, value)
    if not request.path.startswith("/api/v1/integration/"):
        return response

    if response is None:
        logger.exception(
            "Unhandled Integration API exception",
            extra={"request_id": request_id(request)},
        )
        response = Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    value = request_id(request)
    data = _plain(response.data)
    existing = data.get("error") if isinstance(data, dict) else None
    if isinstance(existing, dict):
        error = existing
        error.setdefault("code", "REQUEST_FAILED")
        error.setdefault("category", "validation")
        error.setdefault("message", "请求失败。")
        error.setdefault("retryable", False)
        error.setdefault("details", {})
        retryable = error.get("retryable")
        if isinstance(retryable, str) and retryable.casefold() in {"true", "false"}:
            error["retryable"] = retryable.casefold() == "true"
    else:
        code, category, message, retryable = ERROR_DEFAULTS.get(
            response.status_code,
            (
                "INTERNAL_ERROR",
                "application",
                "服务处理请求时发生内部错误。",
                True,
            ),
        )
        details = {}
        if isinstance(data, dict) and data:
            details["validation"] = data
        error = {
            "code": code,
            "category": category,
            "message": message,
            "retryable": retryable,
            "details": details,
        }
    error["request_id"] = value
    response.data = {"error": error}
    return with_request_id(response, value)

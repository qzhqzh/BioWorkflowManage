from __future__ import annotations

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .request_ids import request_id, with_request_id


def _error(request, code: str, message: str, http_status: int) -> Response:
    value = request_id(request)
    response = Response(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": value,
            }
        },
        status=http_status,
    )
    return with_request_id(response, value)


def _user_payload(user) -> dict:
    return {
        "id": user.pk,
        "username": user.get_username(),
    }


@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf_token(request):
    value = request_id(request)
    response = Response({"csrf_token": get_token(request)})
    return with_request_id(response, value)


@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    username = str(request.data.get("username") or "").strip()
    password = request.data.get("password")
    if not username or not isinstance(password, str):
        return _error(
            request,
            "AUTH_INVALID_CREDENTIALS",
            "username and password are required.",
            status.HTTP_400_BAD_REQUEST,
        )

    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _error(
            request,
            "AUTH_INVALID_CREDENTIALS",
            "用户名或密码错误。",
            status.HTTP_401_UNAUTHORIZED,
        )

    login(request, user)
    value = request_id(request)
    response = Response(
        {
            "user": _user_payload(user),
            "csrf_token": get_token(request),
        }
    )
    return with_request_id(response, value)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    value = request_id(request)
    response = Response({"user": _user_payload(request.user)})
    return with_request_id(response, value)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout_view(request):
    logout(request)
    value = request_id(request)
    response = Response({"status": "ok"})
    return with_request_id(response, value)

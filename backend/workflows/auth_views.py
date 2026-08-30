from __future__ import annotations

import hashlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from .auth_roles import allowed_sections, user_role
from .models import LoginRateLimitBucket
from .request_ids import request_id, with_request_id


class LoginRateThrottle(SimpleRateThrottle):
    scope = "login"

    def allow_request(self, request, view) -> bool:
        if self.rate is None:
            return True
        ident = self.get_ident(request)
        key = hashlib.sha256(f"{self.scope}:{ident}".encode()).hexdigest()
        now = timezone.now()
        LoginRateLimitBucket.objects.filter(
            updated_at__lt=now
            - timedelta(days=settings.LOGIN_THROTTLE_RETENTION_DAYS)
        ).delete()
        with transaction.atomic():
            bucket, _created = LoginRateLimitBucket.objects.select_for_update().get_or_create(
                key=key,
                defaults={
                    "window_started_at": now,
                    "request_count": 0,
                },
            )
            elapsed = (now - bucket.window_started_at).total_seconds()
            if elapsed < 0 or elapsed >= self.duration:
                bucket.window_started_at = now
                bucket.request_count = 0
                elapsed = 0
            self._wait_seconds = max(0.0, self.duration - elapsed)
            if bucket.request_count >= self.num_requests:
                return False
            bucket.request_count += 1
            bucket.save(
                update_fields=[
                    "window_started_at",
                    "request_count",
                    "updated_at",
                ]
            )
        return True

    def wait(self) -> float:
        return getattr(self, "_wait_seconds", self.duration)


def csrf_failure(request, reason="") -> JsonResponse:
    value = request_id(request)
    response = JsonResponse(
        {
            "error": {
                "code": "AUTH_CSRF_FAILED",
                "message": "CSRF 校验失败，请刷新页面后重试。",
                "request_id": value,
            }
        },
        status=status.HTTP_403_FORBIDDEN,
    )
    response["X-Request-ID"] = value
    return response


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
    role = user_role(user)
    return {
        "id": user.pk,
        "username": user.get_username(),
        "is_admin": role == "admin",
        "role": role,
        "allowed_sections": list(allowed_sections(user)),
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
@throttle_classes([LoginRateThrottle])
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


# DRF's ``api_view`` marks its generated view as csrf_exempt.  Clear that marker
# before applying Django's middleware decorator so anonymous session creation is
# protected even though the endpoint intentionally allows unauthenticated users.
login_view.csrf_exempt = False
login_view = csrf_protect(login_view)


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

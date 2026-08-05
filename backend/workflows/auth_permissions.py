from __future__ import annotations

from django.conf import settings
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission


class SessionAuthenticationWithHeader(SessionAuthentication):
    """Session auth that lets DRF report unauthenticated API calls as 401."""

    def authenticate_header(self, request) -> str:
        return "Session"


class AuthenticationRequiredPermission(BasePermission):
    """Gate API access behind the runtime ``AUTH_REQUIRED`` switch."""

    message = "Authentication credentials are required."

    def has_permission(self, request, view) -> bool:
        if not settings.AUTH_REQUIRED:
            return True
        user = getattr(request, "user", None)
        return bool(user is not None and user.is_authenticated)

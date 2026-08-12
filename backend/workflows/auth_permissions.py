from __future__ import annotations

from django.conf import settings
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission

from .auth_roles import is_admin, is_analysis_operator, is_workflow_maintainer


class SessionAuthenticationWithHeader(SessionAuthentication):
    """Session auth that lets DRF report unauthenticated API calls as 401."""

    def authenticate_header(self, request) -> str:
        return "Session"


class AuthenticationRequiredPermission(BasePermission):
    """Require authentication and enforce the configured product role."""

    message = "Authentication credentials are required."

    def has_permission(self, request, view) -> bool:
        if not settings.AUTH_REQUIRED:
            return True
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            self.message = "Authentication credentials are required."
            return False
        if is_admin(user) or is_workflow_maintainer(user):
            return True
        allowed = bool(
            getattr(view, "analysis_operator_allowed", False)
            and is_analysis_operator(user)
        )
        if not allowed:
            self.message = "当前用户没有权限访问此功能。"
        return allowed

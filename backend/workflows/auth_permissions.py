from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from .auth_roles import is_admin, is_analysis_operator, is_workflow_maintainer
from .models import ServiceAccount, ServiceToken


@dataclass(frozen=True)
class ServicePrincipal:
    service_account: ServiceAccount

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    @property
    def is_staff(self) -> bool:
        return False

    @property
    def is_superuser(self) -> bool:
        return False

    def get_username(self) -> str:
        return f"service:{self.service_account.client_id}"


def service_token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authentication_error(code: str, message: str) -> dict:
    return {
        "error": {
            "code": code,
            "category": "authentication",
            "message": message,
            "retryable": False,
            "details": {},
        }
    }


class ServiceTokenAuthentication(BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        value = request.headers.get("Authorization", "").strip()
        if not value:
            return None
        parts = value.split()
        if len(parts) != 2 or parts[0].casefold() != self.keyword.casefold():
            raise AuthenticationFailed(
                _authentication_error(
                    "SERVICE_TOKEN_INVALID",
                    "Authorization 必须使用 Bearer Token。",
                )
            )
        raw_token = parts[1]
        if not raw_token.startswith("bwm_") or len(raw_token) > 256:
            raise AuthenticationFailed(
                _authentication_error("SERVICE_TOKEN_INVALID", "Service Token 无效。")
            )
        token = (
            ServiceToken.objects.select_related("service_account")
            .filter(token_hash=service_token_digest(raw_token))
            .first()
        )
        now = timezone.now()
        if (
            token is None
            or token.revoked_at is not None
            or not token.service_account.is_active
            or (token.expires_at is not None and token.expires_at <= now)
        ):
            raise AuthenticationFailed(
                _authentication_error(
                    "SERVICE_TOKEN_INVALID",
                    "Service Token 无效、已过期或已被吊销。",
                )
            )
        if token.last_used_at is None or token.last_used_at < now - timedelta(minutes=5):
            ServiceToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return ServicePrincipal(token.service_account), token

    def authenticate_header(self, request) -> str:
        return self.keyword


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
        if isinstance(user, ServicePrincipal):
            self.message = "Service Token 只能访问 Integration API。"
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


class IntegrationScopePermission(BasePermission):
    """Allow a scoped service identity or a browser administrator."""

    def has_permission(self, request, view) -> bool:
        by_method = getattr(view, "required_service_scopes_by_method", {})
        required = set(
            by_method.get(request.method, getattr(view, "required_service_scopes", ()))
        )
        token = request.auth if isinstance(request.auth, ServiceToken) else None
        if token is not None:
            account_scopes = set(token.service_account.scopes or [])
            if required.issubset(account_scopes):
                return True
            self.message = {
                "error": {
                    "code": "SERVICE_SCOPE_REQUIRED",
                    "category": "authorization",
                    "message": "Service Account 缺少所需权限。",
                    "retryable": False,
                    "details": {"required_scopes": sorted(required)},
                }
            }
            return False
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False) and is_admin(user):
            return True
        self.message = {
            "error": {
                "code": "SERVICE_AUTHENTICATION_REQUIRED",
                "category": "authentication",
                "message": "该接口需要 Service Token 或管理员会话。",
                "retryable": False,
                "details": {},
            }
        }
        return False


def require_service_scopes(*scopes: str):
    def decorator(view):
        view.view_class.required_service_scopes = tuple(scopes)
        return view

    return decorator


def require_service_scopes_by_method(**mapping):
    def decorator(view):
        view.view_class.required_service_scopes_by_method = {
            method.upper(): tuple(scopes)
            for method, scopes in mapping.items()
        }
        return view

    return decorator

from __future__ import annotations

import secrets
from datetime import datetime

from django.db import transaction

from .auth_permissions import service_token_digest
from .models import ServiceAccount, ServiceToken


SERVICE_SCOPES = frozenset(
    {
        "analysis:submit",
        "analysis:read",
        "analysis:cancel",
        "analysis:retry",
        "analysis:download",
        "workflow:read",
        "library:read",
        "task:test",
    }
)


def validate_service_scopes(scopes: list[str] | tuple[str, ...]) -> list[str]:
    normalized = sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
    unknown = sorted(set(normalized) - SERVICE_SCOPES)
    if unknown:
        raise ValueError(f"未知 Service Account scope：{', '.join(unknown)}")
    return normalized


def issue_service_token(
    account: ServiceAccount,
    *,
    name: str,
    actor: str,
    expires_at: datetime | None = None,
) -> tuple[ServiceToken, str]:
    prefix = secrets.token_hex(8)
    raw_token = f"bwm_{prefix}_{secrets.token_urlsafe(32)}"
    with transaction.atomic():
        token = ServiceToken.objects.create(
            service_account=account,
            name=name,
            prefix=prefix,
            token_hash=service_token_digest(raw_token),
            created_by=actor,
            expires_at=expires_at,
        )
    return token, raw_token

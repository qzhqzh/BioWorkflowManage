from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .integration_outputs import ResourceSnapshotBudgetError
from .resource_catalog import (
    ResourceCatalogError,
    catalog_payload,
    save_catalog,
)


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_username()
    return "local-user"


def _error(error: ResourceCatalogError, http_status: int) -> Response:
    payload = {"code": error.code, "message": str(error)}
    if error.details is not None:
        payload["details"] = error.details
    return Response({"error": payload}, status=http_status)


@api_view(["GET", "PUT"])
def resource_catalog(request):
    if request.method == "GET":
        try:
            verify_kind = str(request.query_params.get("verify_kind") or "")
            verify_id = str(request.query_params.get("verify_id") or "")
            verify_entry = None
            if verify_kind or verify_id:
                if verify_kind not in {"references", "panels"} or not verify_id:
                    raise ResourceCatalogError(
                        "RESOURCE_CATALOG_VERIFY_INVALID",
                        "完整性校验需要有效的资源类型和资源 ID。",
                    )
                state = catalog_payload()
                if not any(item["id"] == verify_id for item in state[verify_kind]):
                    raise ResourceCatalogError(
                        "RESOURCE_CATALOG_ENTRY_NOT_FOUND",
                        "待校验的资源不存在，请刷新目录。",
                    )
                verify_entry = (verify_kind, verify_id)
            return Response(catalog_payload(verify_entry=verify_entry))
        except ResourceCatalogError as error:
            return _error(error, status.HTTP_400_BAD_REQUEST)
        except ResourceSnapshotBudgetError as error:
            return _error(
                ResourceCatalogError(
                    "RESOURCE_CATALOG_VERIFY_LIMIT_EXCEEDED",
                    str(error),
                ),
                status.HTTP_400_BAD_REQUEST,
            )

    if "base_version" not in request.data or not request.data.get("base_digest"):
        return _error(
            ResourceCatalogError(
                "RESOURCE_CATALOG_PRECONDITION_REQUIRED",
                "保存资源目录必须携带 base_version 和 base_digest。",
            ),
            status.HTTP_428_PRECONDITION_REQUIRED,
        )
    try:
        base_version = int(request.data["base_version"])
        save_catalog(
            request.data.get("document"),
            base_version=base_version,
            base_digest=str(request.data["base_digest"]),
            actor=_actor(request),
            note=str(request.data.get("note") or "").strip()[:1000],
        )
        return Response(catalog_payload(), status=status.HTTP_200_OK)
    except ResourceCatalogError as error:
        http_status = (
            status.HTTP_409_CONFLICT
            if error.code == "RESOURCE_CATALOG_CONFLICT"
            else status.HTTP_400_BAD_REQUEST
        )
        return _error(error, http_status)
    except (TypeError, ValueError):
        return _error(
            ResourceCatalogError(
                "RESOURCE_CATALOG_PRECONDITION_INVALID",
                "base_version 必须是整数。",
            ),
            status.HTTP_400_BAD_REQUEST,
        )

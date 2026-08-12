from __future__ import annotations

from django.db.models import Count, OuterRef, Subquery
from rest_framework.decorators import api_view
from rest_framework.response import Response

from compiler_core import canonical_digest

from .models import ToolDocument, ToolVersion
from .request_ids import request_id, with_request_id


@api_view(["GET"])
def tools(request):
    value = request_id(request)
    latest_version_pk = (
        ToolVersion.objects.filter(tool_id=OuterRef("tool_id"))
        .order_by("-created_at", "-pk")
        .values("pk")[:1]
    )
    latest_map = {
        item.tool_id: item
        for item in ToolVersion.objects.filter(pk=Subquery(latest_version_pk))
    }
    version_counts = {
        item["tool_id"]: item["version_count"]
        for item in ToolVersion.objects.values("tool_id")
        .annotate(version_count=Count("id"))
        .order_by()
    }
    draft_map = {item.tool_id: item for item in ToolDocument.objects.all()}
    tool_ids = sorted(set(latest_map) | set(draft_map))

    results = []
    for tool_id in tool_ids:
        latest = latest_map.get(tool_id)
        draft = draft_map.get(tool_id)
        draft_spec = draft.draft_spec if draft else {}
        results.append(
            {
                "tool_id": tool_id,
                "name": (
                    (draft.draft_spec.get("display_name") or draft.draft_spec.get("name"))
                    if draft
                    else latest.name
                ),
                "latest_version": latest.version if latest else None,
                "latest_digest": latest.digest if latest else None,
                "latest_created_at": latest.created_at.isoformat() if latest else None,
                "version_count": version_counts.get(tool_id, 0),
                "draft_status": (
                    draft.validation.get("status", "unknown") if draft else None
                ),
                "draft_version": draft_spec.get("tool_version") if draft else None,
                "draft_digest": canonical_digest(draft_spec) if draft else None,
                "draft_updated_at": draft.updated_at.isoformat() if draft else None,
                "description": draft_spec.get("description", ""),
                "category": draft_spec.get("category"),
                "task_kind": draft_spec.get("task_kind", "standard"),
                "source_wdl": draft_spec.get("metadata", {}).get("source_wdl"),
                "migration_warning_count": len(
                    draft_spec.get("metadata", {}).get("migration_warnings", [])
                ),
            }
        )
    return with_request_id(Response({"results": results}), value)

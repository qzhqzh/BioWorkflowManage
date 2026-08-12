from __future__ import annotations

import json
import re
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    SoftwareAsset,
    SoftwareAuditEvent,
    SoftwareRelease,
    ToolSoftwareLink,
    ToolVersion,
)


SOFTWARE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
ASSET_FIELDS = (
    "name",
    "summary",
    "description",
    "homepage",
    "source_repository",
    "license",
    "notes",
    "tags",
    "metadata",
    "lifecycle",
)
RELEASE_FIELDS = ("description", "container_images", "metadata")


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_username()
    return "local-user"


def _error(code: str, message: str, http_status: int, *, details=None) -> Response:
    payload: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        payload["details"] = details
    return Response({"error": payload}, status=http_status)


def _clean_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("标签必须是数组。")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = str(raw).strip()
        key = item.casefold()
        if not item or len(item) > 64:
            raise ValueError("标签不能为空且不能超过 64 个字符。")
        if key not in seen:
            result.append(item)
            seen.add(key)
    if len(result) > 64:
        raise ValueError("标签不能超过 64 个。")
    return result


def _validate_asset_values(
    values: dict[str, Any], *, require_name: bool = False
) -> dict[str, Any]:
    cleaned = {field: values[field] for field in ASSET_FIELDS if field in values}
    for field in (
        "name",
        "summary",
        "description",
        "homepage",
        "source_repository",
        "license",
        "notes",
        "lifecycle",
    ):
        if field in cleaned:
            cleaned[field] = str(cleaned[field]).strip()
    if require_name and not cleaned.get("name"):
        raise ValueError("软件名称不能为空。")
    if "name" in cleaned and not cleaned["name"]:
        raise ValueError("软件名称不能为空。")
    limits = {
        "name": 256,
        "summary": 512,
        "homepage": 2048,
        "source_repository": 512,
        "license": 128,
    }
    if any(len(cleaned.get(field, "")) > limit for field, limit in limits.items()):
        raise ValueError("软件信息字段长度超出限制。")
    if "lifecycle" in cleaned and cleaned["lifecycle"] not in SoftwareAsset.Lifecycle.values:
        raise ValueError("软件生命周期无效。")
    if "tags" in cleaned:
        cleaned["tags"] = _clean_tags(cleaned["tags"])
    if "metadata" in cleaned and not isinstance(cleaned["metadata"], dict):
        raise ValueError("扩展信息必须是 JSON object。")
    return cleaned


def _release_payload(item: SoftwareRelease) -> dict[str, Any]:
    return {
        "id": item.id,
        "version": item.version,
        "description": item.description,
        "container_images": item.container_images,
        "metadata": item.metadata,
        "metadata_version": item.metadata_version,
        "created_by": item.created_by,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _link_payload(item: ToolSoftwareLink) -> dict[str, Any]:
    return {
        "id": item.id,
        "role": item.role,
        "note": item.note,
        "tool": {
            "id": item.tool_version.tool_id,
            "version": item.tool_version.version,
            "name": item.tool_version.name,
            "digest": item.tool_version.digest,
        },
        "release": (
            {"id": item.release_id, "version": item.release.version}
            if item.release_id
            else None
        ),
        "created_by": item.created_by,
        "created_at": item.created_at,
    }


def software_payload(item: SoftwareAsset, *, detail: bool = False) -> dict[str, Any]:
    payload = {
        "slug": item.slug,
        "name": item.name,
        "summary": item.summary,
        "description": item.description,
        "homepage": item.homepage,
        "source_repository": item.source_repository,
        "license": item.license,
        "notes": item.notes,
        "tags": item.tags,
        "metadata": item.metadata,
        "lifecycle": item.lifecycle,
        "metadata_version": item.metadata_version,
        "release_count": item.releases.count(),
        "tool_count": item.tool_links.values("tool_version_id").distinct().count(),
        "created_by": item.created_by,
        "updated_by": item.updated_by,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
    if detail:
        payload["releases"] = [
            _release_payload(release) for release in item.releases.all()
        ]
        payload["tool_links"] = [
            _link_payload(link)
            for link in item.tool_links.select_related("tool_version", "release")
        ]
        payload["audit_events"] = [
            {
                "id": event.id,
                "action": event.action,
                "actor": event.actor,
                "changes": event.changes,
                "note": event.note,
                "created_at": event.created_at,
            }
            for event in item.audit_events.all()[:50]
        ]
    return payload


@api_view(["GET", "POST"])
def software_assets(request):
    if request.method == "GET":
        query = str(request.query_params.get("q") or "").strip()
        items = SoftwareAsset.objects.all()
        if query:
            items = items.filter(name__icontains=query) | items.filter(
                slug__icontains=query
            )
        return Response({"results": [software_payload(item) for item in items]})

    slug = str(request.data.get("slug") or "").strip().lower()
    if not SOFTWARE_SLUG.fullmatch(slug):
        return _error(
            "SOFTWARE_SLUG_INVALID",
            "软件 ID 只能包含小写字母、数字、下划线和连字符。",
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        values = _validate_asset_values(dict(request.data), require_name=True)
    except ValueError as error:
        return _error("SOFTWARE_INVALID", str(error), status.HTTP_400_BAD_REQUEST)
    actor = _actor(request)
    try:
        with transaction.atomic():
            item = SoftwareAsset.objects.create(
                slug=slug,
                created_by=actor,
                updated_by=actor,
                **values,
            )
            SoftwareAuditEvent.objects.create(
                software=item,
                action="create",
                actor=actor,
                changes={"after": _json_value(software_payload(item))},
            )
    except IntegrityError:
        return _error(
            "SOFTWARE_EXISTS",
            "该软件 ID 已存在。",
            status.HTTP_409_CONFLICT,
        )
    return Response(software_payload(item, detail=True), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
def software_asset_detail(request, slug: str):
    item = get_object_or_404(SoftwareAsset, slug=slug)
    if request.method == "GET":
        return Response(software_payload(item, detail=True))
    base_version = request.data.get("base_metadata_version")
    if base_version is None:
        return _error(
            "SOFTWARE_PRECONDITION_REQUIRED",
            "修改软件信息必须携带当前 metadata version。",
            428,
        )
    try:
        incoming = _validate_asset_values(dict(request.data))
    except ValueError as error:
        return _error("SOFTWARE_INVALID", str(error), status.HTTP_400_BAD_REQUEST)
    actor = _actor(request)
    with transaction.atomic():
        item = SoftwareAsset.objects.select_for_update().get(pk=item.pk)
        if base_version != item.metadata_version:
            return _error(
                "SOFTWARE_CONFLICT",
                "软件信息已被其他用户修改，请刷新后重试。",
                status.HTTP_409_CONFLICT,
                details={"current_metadata_version": item.metadata_version},
            )
        before = {field: getattr(item, field) for field in incoming}
        changed = {
            field: value
            for field, value in incoming.items()
            if getattr(item, field) != value
        }
        if changed:
            for field, value in changed.items():
                setattr(item, field, value)
            item.updated_by = actor
            item.metadata_version += 1
            item.save(
                update_fields=[*changed, "updated_by", "metadata_version", "updated_at"]
            )
            SoftwareAuditEvent.objects.create(
                software=item,
                action="update",
                actor=actor,
                changes={
                    "before": {field: before[field] for field in changed},
                    "after": changed,
                },
                note=str(request.data.get("note") or "").strip(),
            )
    return Response(software_payload(item, detail=True))


@api_view(["GET", "POST"])
def software_releases(request, slug: str):
    software = get_object_or_404(SoftwareAsset, slug=slug)
    if request.method == "GET":
        return Response(
            {"results": [_release_payload(item) for item in software.releases.all()]}
        )
    version = str(request.data.get("version") or "").strip()
    if not version or len(version) > 128:
        return _error(
            "SOFTWARE_RELEASE_INVALID",
            "请填写有效的软件版本。",
            status.HTTP_400_BAD_REQUEST,
        )
    container_images = request.data.get("container_images") or []
    metadata = request.data.get("metadata") or {}
    if not isinstance(container_images, list) or not all(
        isinstance(image, str) and image.strip() for image in container_images
    ):
        return _error(
            "SOFTWARE_RELEASE_INVALID",
            "容器镜像必须是非空字符串数组。",
            status.HTTP_400_BAD_REQUEST,
        )
    if not isinstance(metadata, dict):
        return _error(
            "SOFTWARE_RELEASE_INVALID",
            "版本扩展信息必须是 JSON object。",
            status.HTTP_400_BAD_REQUEST,
        )
    actor = _actor(request)
    try:
        with transaction.atomic():
            item = SoftwareRelease.objects.create(
                software=software,
                version=version,
                description=str(request.data.get("description") or "").strip(),
                container_images=[image.strip() for image in container_images],
                metadata=metadata,
                created_by=actor,
            )
            SoftwareAuditEvent.objects.create(
                software=software,
                release=item,
                action="release_create",
                actor=actor,
                changes={"after": _json_value(_release_payload(item))},
            )
    except IntegrityError:
        return _error(
            "SOFTWARE_RELEASE_EXISTS",
            "该软件版本已存在。",
            status.HTTP_409_CONFLICT,
        )
    return Response(_release_payload(item), status=status.HTTP_201_CREATED)


@api_view(["PATCH"])
def software_release_detail(request, slug: str, release_id: int):
    software = get_object_or_404(SoftwareAsset, slug=slug)
    release = get_object_or_404(SoftwareRelease, software=software, pk=release_id)
    base_version = request.data.get("base_metadata_version")
    if base_version is None:
        return _error(
            "SOFTWARE_RELEASE_PRECONDITION_REQUIRED",
            "修改软件版本必须携带当前 metadata version。",
            428,
        )
    incoming = {
        field: request.data[field]
        for field in RELEASE_FIELDS
        if field in request.data
    }
    if "description" in incoming:
        incoming["description"] = str(incoming["description"]).strip()
    if "container_images" in incoming and (
        not isinstance(incoming["container_images"], list)
        or not all(isinstance(value, str) and value.strip() for value in incoming["container_images"])
    ):
        return _error(
            "SOFTWARE_RELEASE_INVALID",
            "容器镜像必须是非空字符串数组。",
            status.HTTP_400_BAD_REQUEST,
        )
    if "metadata" in incoming and not isinstance(incoming["metadata"], dict):
        return _error(
            "SOFTWARE_RELEASE_INVALID",
            "版本扩展信息必须是 JSON object。",
            status.HTTP_400_BAD_REQUEST,
        )
    actor = _actor(request)
    with transaction.atomic():
        release = SoftwareRelease.objects.select_for_update().get(pk=release.pk)
        if base_version != release.metadata_version:
            return _error(
                "SOFTWARE_RELEASE_CONFLICT",
                "软件版本信息已变化，请刷新后重试。",
                status.HTTP_409_CONFLICT,
            )
        before = {field: getattr(release, field) for field in incoming}
        changed = {
            field: value
            for field, value in incoming.items()
            if getattr(release, field) != value
        }
        if changed:
            for field, value in changed.items():
                setattr(release, field, value)
            release.metadata_version += 1
            release.save(update_fields=[*changed, "metadata_version", "updated_at"])
            SoftwareAuditEvent.objects.create(
                software=software,
                release=release,
                action="release_update",
                actor=actor,
                changes={
                    "before": {field: before[field] for field in changed},
                    "after": changed,
                },
            )
    return Response(_release_payload(release))


@api_view(["POST"])
def software_tool_links(request, slug: str):
    software = get_object_or_404(SoftwareAsset, slug=slug)
    tool_id = str(request.data.get("tool_id") or "").strip()
    version = str(request.data.get("tool_version") or "").strip()
    tool_version = get_object_or_404(ToolVersion, tool_id=tool_id, version=version)
    role = str(request.data.get("role") or ToolSoftwareLink.Role.PRIMARY)
    if role not in ToolSoftwareLink.Role.values:
        return _error(
            "SOFTWARE_LINK_INVALID",
            "工具关联角色无效。",
            status.HTTP_400_BAD_REQUEST,
        )
    release = None
    release_version = str(request.data.get("software_version") or "").strip()
    if release_version:
        release = software.releases.filter(version=release_version).first()
        if release is None:
            return _error(
                "SOFTWARE_RELEASE_NOT_FOUND",
                "所选软件版本不存在。",
                status.HTTP_400_BAD_REQUEST,
            )
    actor = _actor(request)
    try:
        with transaction.atomic():
            link = ToolSoftwareLink.objects.create(
                tool_version=tool_version,
                software=software,
                release=release,
                role=role,
                note=str(request.data.get("note") or "").strip(),
                created_by=actor,
            )
            SoftwareAuditEvent.objects.create(
                software=software,
                release=release,
                action="tool_link",
                actor=actor,
                changes={"after": _json_value(_link_payload(link))},
            )
    except IntegrityError:
        return _error(
            "SOFTWARE_LINK_EXISTS",
            "该工具版本已存在相同角色的软件关联。",
            status.HTTP_409_CONFLICT,
        )
    return Response(_link_payload(link), status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def software_tool_link_detail(request, slug: str, link_id: int):
    software = get_object_or_404(SoftwareAsset, slug=slug)
    link = get_object_or_404(
        ToolSoftwareLink.objects.select_related("tool_version", "release"),
        software=software,
        pk=link_id,
    )
    actor = _actor(request)
    with transaction.atomic():
        before = _json_value(_link_payload(link))
        SoftwareAuditEvent.objects.create(
            software=software,
            release=link.release,
            action="tool_unlink",
            actor=actor,
            changes={"before": before},
        )
        link.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)

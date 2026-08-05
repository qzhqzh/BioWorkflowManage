from __future__ import annotations

import json
import re
import uuid

from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    WDLToolPackage,
    WDLToolPackageAuditEvent,
    WDLToolPackageFile,
    WDLToolPackageTag,
    WDLToolPackageVersion,
)
from .request_ids import request_id, with_request_id
from .wdl_packages import (
    WDLPackageError,
    analyze_wdl_library,
    build_wdl_library_archive,
    normalize_bundle_files,
    package_digest,
    read_wdl_library_archive,
)
from .wdl_source_references import current_source_references


VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        username = user.get_username()
        if username:
            return username[:256]
    return "local-user"


def _error(request, code: str, message: str, http_status: int, *, details=None):
    value = request_id(request)
    response = Response(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": value,
                **({"details": details} if details else {}),
            }
        },
        status=http_status,
    )
    return with_request_id(response, value)


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _tag_names(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    names = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            return None
        name = item.strip()
        key = name.casefold()
        if not name or len(name) > 64 or key in seen:
            continue
        names.append(name)
        seen.add(key)
    return names


def _set_tags(package: WDLToolPackage, names: list[str]) -> list[str]:
    tags = []
    for name in names:
        tag = WDLToolPackageTag.objects.filter(name__iexact=name).first()
        if tag is None:
            tag = WDLToolPackageTag.objects.create(name=name)
        tags.append(tag)
    package.tags.set(tags)
    return [tag.name for tag in tags]


def _unique_slug(name: str, requested_slug: str | None = None) -> str:
    base = slugify(requested_slug or name)[:112] or f"wdl-package-{uuid.uuid4().hex[:8]}"
    candidate = base
    suffix = 2
    while WDLToolPackage.objects.filter(slug=candidate).exists():
        candidate = f"{base[:112]}-{suffix}"
        suffix += 1
    return candidate


def _request_files(request) -> dict[str, str]:
    archive = request.FILES.get("archive")
    if archive is not None:
        return read_wdl_library_archive(archive)
    raw_files = request.data.get("files")
    if isinstance(raw_files, list) and raw_files:
        first_path = str(raw_files[0].get("path") or "") if isinstance(raw_files[0], dict) else ""
        files, _ = normalize_bundle_files(raw_files, first_path)
        return files
    raise WDLPackageError(
        "WDL_PACKAGE_FILES_INVALID",
        "A ZIP archive containing WDL files is required.",
    )


def _file_payload(item: WDLToolPackageFile, *, include_content: bool = False) -> dict:
    return {
        "path": item.path,
        "digest": item.digest,
        "analysis": item.analysis,
        **({"content": item.content} if include_content else {}),
    }


def _version_payload(
    item: WDLToolPackageVersion,
    *,
    include_content: bool = False,
) -> dict:
    return {
        "version": item.version,
        "digest": item.digest,
        "source_repository": item.source_repository,
        "source_revision": item.source_revision,
        "note": item.note,
        "actor": item.actor,
        "analysis": item.analysis,
        "file_count": item.files.count(),
        "files": [
            _file_payload(file, include_content=include_content)
            for file in item.files.all()
        ],
        "created_at": item.created_at.isoformat(),
    }


def _audit_payload(item: WDLToolPackageAuditEvent) -> dict:
    return {
        "id": item.id,
        "action": item.action,
        "actor": item.actor,
        "note": item.note,
        "changes": item.changes,
        "version": item.package_version.version if item.package_version_id else None,
        "created_at": item.created_at.isoformat(),
    }


def _package_payload(item: WDLToolPackage, *, include_detail: bool = False) -> dict:
    latest = item.versions.first()
    references = current_source_references().filter(package_version__package=item)
    payload = {
        "slug": item.slug,
        "name": item.name,
        "description": item.description,
        "lifecycle": item.lifecycle,
        "tags": [tag.name for tag in item.tags.all()],
        "created_by": item.created_by,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "version_count": item.versions.count(),
        "reference_count": references.count(),
        "latest_version": _version_payload(latest) if latest else None,
    }
    if include_detail:
        payload["versions"] = [_version_payload(version) for version in item.versions.all()[:100]]
        payload["audit_events"] = [
            _audit_payload(event)
            for event in item.audit_events.select_related("package_version").all()[:200]
        ]
        payload["references"] = [
            {
                "asset_slug": reference.revision.asset.slug,
                "asset_name": reference.revision.asset.name,
                "asset_lifecycle": reference.revision.asset.lifecycle,
                "revision": reference.revision.version,
                "package_version": reference.package_version.version,
                "mount_prefix": reference.mount_prefix,
                "digest": reference.digest,
                "created_at": reference.created_at.isoformat(),
            }
            for reference in references.order_by(
                "revision__asset__name", "revision__asset__slug"
            )
        ]
    return payload


def _create_version(request, package: WDLToolPackage):
    version = str(request.data.get("version") or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        return None, False, _error(
            request,
            "WDL_TOOL_PACKAGE_VERSION_INVALID",
            "version must contain only letters, numbers, dots, underscores, pluses, or hyphens.",
            status.HTTP_400_BAD_REQUEST,
        )
    try:
        files = _request_files(request)
        analysis = analyze_wdl_library(files)
    except WDLPackageError as error:
        return None, False, _error(
            request,
            error.code,
            error.message,
            status.HTTP_400_BAD_REQUEST,
            details=error.details,
        )
    if analysis["summary"]["task_count"] == 0:
        return None, False, _error(
            request,
            "WDL_TOOL_PACKAGE_HAS_NO_TASKS",
            "A WDL tool package must expose at least one task.",
            status.HTTP_400_BAD_REQUEST,
        )

    content_digest = package_digest(files)
    existing = package.versions.filter(version=version).first()
    if existing is not None:
        if existing.digest == content_digest:
            return existing, False, None
        return None, False, _error(
            request,
            "WDL_TOOL_PACKAGE_VERSION_CONFLICT",
            "This package version already exists with different content.",
            status.HTTP_409_CONFLICT,
        )

    actor = _actor(request)
    source_repository = str(request.data.get("source_repository") or "").strip()[:512]
    source_revision = str(request.data.get("source_revision") or "").strip()[:128]
    note = str(request.data.get("note") or "").strip()
    package_version = WDLToolPackageVersion.objects.create(
        package=package,
        version=version,
        digest=content_digest,
        source_repository=source_repository,
        source_revision=source_revision,
        note=note,
        actor=actor,
        analysis=analysis,
    )
    file_analysis = {item["path"]: item for item in analysis.get("files", [])}
    WDLToolPackageFile.objects.bulk_create(
        [
            WDLToolPackageFile(
                package_version=package_version,
                path=path,
                content=content,
                digest=file_analysis.get(path, {}).get("digest", ""),
                analysis=file_analysis.get(path, {}),
            )
            for path, content in sorted(files.items())
        ]
    )
    package.updated_at = timezone.now()
    package.save(update_fields=["updated_at"])
    WDLToolPackageAuditEvent.objects.create(
        package=package,
        package_version=package_version,
        action="publish_version",
        actor=actor,
        note=note,
        changes={
            "version": version,
            "digest": content_digest,
            "file_count": len(files),
            "task_count": analysis["summary"]["task_count"],
            "source_repository": source_repository,
            "source_revision": source_revision,
        },
    )
    return package_version, True, None


@api_view(["GET", "POST"])
def wdl_tool_packages(request):
    value = request_id(request)
    if request.method == "GET":
        packages = WDLToolPackage.objects.prefetch_related("tags", "versions__files")
        query = str(request.query_params.get("q") or "").strip()
        if query:
            packages = packages.filter(
                Q(name__icontains=query)
                | Q(slug__icontains=query)
                | Q(description__icontains=query)
                | Q(versions__source_repository__icontains=query)
            ).distinct()
        lifecycle = str(request.query_params.get("lifecycle") or "").strip()
        if lifecycle:
            packages = packages.filter(lifecycle=lifecycle)
        tags = [item for item in request.query_params.getlist("tag") if item]
        if tags:
            packages = packages.filter(tags__name__in=tags).distinct()
        return with_request_id(
            Response({"results": [_package_payload(item) for item in packages]}),
            value,
        )

    name = str(request.data.get("name") or "").strip()
    description = str(request.data.get("description") or "").strip()
    tags = _tag_names(_json_list(request.data.get("tags")))
    if not name or len(name) > 256 or tags is None:
        return _error(
            request,
            "WDL_TOOL_PACKAGE_METADATA_INVALID",
            "name and valid tags are required.",
            status.HTTP_400_BAD_REQUEST,
        )
    with transaction.atomic():
        package = WDLToolPackage.objects.create(
            slug=_unique_slug(name, request.data.get("slug")),
            name=name,
            description=description,
            created_by=_actor(request),
        )
        canonical_tags = _set_tags(package, tags)
        package_version, _, error_response = _create_version(request, package)
        if error_response is not None:
            transaction.set_rollback(True)
            return error_response
        WDLToolPackageAuditEvent.objects.create(
            package=package,
            package_version=package_version,
            action="create_package",
            actor=_actor(request),
            note=str(request.data.get("note") or "").strip(),
            changes={"tags": {"before": [], "after": canonical_tags}},
        )
    return with_request_id(
        Response(_package_payload(package, include_detail=True), status=status.HTTP_201_CREATED),
        value,
    )


@api_view(["GET"])
def wdl_tool_package_tags(request):
    value = request_id(request)
    tags = WDLToolPackageTag.objects.annotate(package_count=Count("packages")).order_by(
        "-package_count", "name"
    )
    return with_request_id(
        Response(
            {
                "results": [
                    {"id": tag.id, "name": tag.name, "package_count": tag.package_count}
                    for tag in tags
                ]
            }
        ),
        value,
    )


@api_view(["GET", "PATCH"])
def wdl_tool_package_detail(request, slug: str):
    package = (
        WDLToolPackage.objects.prefetch_related("tags", "versions__files")
        .filter(slug=slug)
        .first()
    )
    if package is None:
        return _error(
            request,
            "WDL_TOOL_PACKAGE_NOT_FOUND",
            "WDL tool package not found.",
            status.HTTP_404_NOT_FOUND,
        )
    value = request_id(request)
    if request.method == "GET":
        return with_request_id(Response(_package_payload(package, include_detail=True)), value)

    changes = {}
    if "name" in request.data:
        name = str(request.data.get("name") or "").strip()
        if not name or len(name) > 256:
            return _error(
                request,
                "WDL_TOOL_PACKAGE_METADATA_INVALID",
                "name is invalid.",
                status.HTTP_400_BAD_REQUEST,
            )
        if name != package.name:
            changes["name"] = {"before": package.name, "after": name}
            package.name = name
    if "description" in request.data:
        description = str(request.data.get("description") or "").strip()
        if description != package.description:
            changes["description"] = {"before": package.description, "after": description}
            package.description = description
    if "lifecycle" in request.data:
        lifecycle = str(request.data.get("lifecycle") or "")
        if lifecycle not in WDLToolPackage.Lifecycle.values:
            return _error(
                request,
                "WDL_TOOL_PACKAGE_METADATA_INVALID",
                "lifecycle is invalid.",
                status.HTTP_400_BAD_REQUEST,
            )
        if lifecycle != package.lifecycle:
            changes["lifecycle"] = {"before": package.lifecycle, "after": lifecycle}
            package.lifecycle = lifecycle
    if "tags" in request.data:
        tags = _tag_names(request.data.get("tags"))
        if tags is None:
            return _error(
                request,
                "WDL_TOOL_PACKAGE_METADATA_INVALID",
                "tags must be an array of names.",
                status.HTTP_400_BAD_REQUEST,
            )
        previous = [tag.name for tag in package.tags.all()]
        canonical = _set_tags(package, tags)
        if {item.casefold() for item in previous} != {item.casefold() for item in canonical}:
            changes["tags"] = {"before": previous, "after": canonical}
    if changes:
        package.save()
        WDLToolPackageAuditEvent.objects.create(
            package=package,
            action="metadata_update",
            actor=_actor(request),
            note=str(request.data.get("note") or "").strip(),
            changes=changes,
        )
    return with_request_id(Response(_package_payload(package, include_detail=True)), value)


@api_view(["GET", "POST"])
def wdl_tool_package_versions(request, slug: str):
    package = WDLToolPackage.objects.filter(slug=slug).first()
    if package is None:
        return _error(
            request,
            "WDL_TOOL_PACKAGE_NOT_FOUND",
            "WDL tool package not found.",
            status.HTTP_404_NOT_FOUND,
        )
    value = request_id(request)
    if request.method == "GET":
        return with_request_id(
            Response({"results": [_version_payload(item) for item in package.versions.all()]}),
            value,
        )
    if package.lifecycle == WDLToolPackage.Lifecycle.ARCHIVED:
        return _error(
            request,
            "WDL_TOOL_PACKAGE_ARCHIVED",
            "Archived packages cannot publish new versions.",
            status.HTTP_409_CONFLICT,
        )
    with transaction.atomic():
        package_version, created, error_response = _create_version(request, package)
        if error_response is not None:
            return error_response
    return with_request_id(
        Response(
            _version_payload(package_version, include_content=True),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        ),
        value,
    )


@api_view(["GET"])
def wdl_tool_package_version_detail(request, slug: str, version: str):
    item = (
        WDLToolPackageVersion.objects.select_related("package")
        .prefetch_related("files")
        .filter(package__slug=slug, version=version)
        .first()
    )
    if item is None:
        return _error(
            request,
            "WDL_TOOL_PACKAGE_VERSION_NOT_FOUND",
            "WDL tool package version not found.",
            status.HTTP_404_NOT_FOUND,
        )
    return with_request_id(
        Response(_version_payload(item, include_content=True)),
        request_id(request),
    )


@api_view(["GET"])
def export_wdl_tool_package(request, slug: str):
    package = WDLToolPackage.objects.filter(slug=slug).first()
    if package is None:
        return _error(
            request,
            "WDL_TOOL_PACKAGE_NOT_FOUND",
            "WDL tool package not found.",
            status.HTTP_404_NOT_FOUND,
        )
    version = str(request.query_params.get("version") or "").strip()
    package_version = (
        package.versions.filter(version=version).first() if version else package.versions.first()
    )
    if package_version is None:
        return _error(
            request,
            "WDL_TOOL_PACKAGE_VERSION_NOT_FOUND",
            "WDL tool package version not found.",
            status.HTTP_404_NOT_FOUND,
        )
    files = {item.path: item.content for item in package_version.files.all()}
    archive = build_wdl_library_archive(
        files,
        package={
            "name": package.name,
            "slug": package.slug,
            "version": package_version.version,
            "digest": package_version.digest,
            "sourceRepository": package_version.source_repository,
            "sourceRevision": package_version.source_revision,
        },
    )
    response = HttpResponse(archive, content_type="application/zip")
    response["Content-Disposition"] = (
        f'attachment; filename="{package.slug}-{package_version.version}.zip"'
    )
    response["X-Request-ID"] = request_id(request)
    return response

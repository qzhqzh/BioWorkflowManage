from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
import tempfile
import uuid
from collections import Counter
from pathlib import Path

import WDL
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import WDLAuditEvent, WDLAsset, WDLSourceFile, WDLSourceRevision, WDLTag
from .wdl_packages import (
    WDLPackageError,
    analyze_wdl_bundle,
    build_wdl_archive,
    digest,
    normalize_bundle_files,
    read_wdl_archive,
)
from .wdl_source_references import (
    PackageReferenceSpec,
    effective_package_files,
    package_reference_payload,
    parse_reference_specs,
    persist_reference_specs,
    reference_spec_key,
    reference_specs_for_revision,
)
from .wdl_task_import import (
    ResolvedWDLTaskSource,
    WDLTaskImportError,
    import_task_as_tool_draft,
)


MAX_WDL_CONTENT_LENGTH = 2_000_000
REVISION_REFERENCE_PREFETCHES = (
    "package_references__package_version__package",
    "package_references__package_version__files",
)
ASSET_REVISION_PREFETCHES = (
    "source_revisions__files",
    "source_revisions__package_references__package_version__package",
    "source_revisions__package_references__package_version__files",
)


class WDLFormatterUnavailable(RuntimeError):
    pass


class WDLFormatterRejected(RuntimeError):
    pass


def _request_id(request) -> str:
    candidate = request.headers.get("X-Request-ID", "")
    if candidate and len(candidate) <= 128 and candidate.replace("-", "_").isalnum():
        return candidate
    return f"req_{uuid.uuid4().hex}"


def _with_request_id(response: Response, request_id: str) -> Response:
    response["X-Request-ID"] = request_id
    return response


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        username = user.get_username()
        if username:
            return username[:256]
    return "local-user"


def _digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _normalize_newlines(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _source_position(item) -> dict:
    position = getattr(item, "pos", None)
    if position is None:
        return {}
    return {
        "line": getattr(position, "line", None),
        "column": getattr(position, "column", None),
        "end_line": getattr(position, "end_line", None),
        "end_column": getattr(position, "end_column", None),
    }


def _declaration_payload(declaration) -> dict:
    return {
        "name": declaration.name,
        "type": str(getattr(declaration, "type", "")),
        **_source_position(declaration),
    }


def _diagnostic_from_error(error: Exception, code: str) -> dict:
    position = getattr(error, "pos", None)
    location = {}
    if position is not None:
        location = {
            "line": getattr(position, "line", None),
            "column": getattr(position, "column", None),
        }
    return {
        "code": code,
        "stage": "wdl_analysis",
        "severity": "error",
        "message": str(error).strip() or type(error).__name__,
        **({"location": location} if any(location.values()) else {}),
    }


def analyze_wdl(content: str, filename: str) -> dict:
    normalized_content = _normalize_newlines(content)
    try:
        document = WDL.parse_document(normalized_content, uri=filename)
    except Exception as error:
        diagnostics = [_diagnostic_from_error(error, "WDL_PARSE_ERROR")]
        return {
            "status": "invalid",
            "parsed": False,
            "wdl_version": None,
            "summary": {
                "task_count": 0,
                "workflow_count": 0,
                "import_count": 0,
                "error_count": len(diagnostics),
            },
            "imports": [],
            "tasks": [],
            "workflows": [],
            "diagnostics": diagnostics,
        }

    diagnostics = []
    try:
        document.typecheck()
    except Exception as error:
        diagnostics.append(_diagnostic_from_error(error, "WDL_TYPE_ERROR"))

    tasks = []
    for task in document.tasks:
        inputs = [
            *(getattr(task, "inputs", None) or []),
            *(getattr(task, "postinputs", None) or []),
        ]
        tasks.append(
            {
                "name": task.name,
                **_source_position(task),
                "inputs": [_declaration_payload(item) for item in inputs],
                "outputs": [
                    _declaration_payload(item)
                    for item in (getattr(task, "outputs", None) or [])
                ],
                "runtime_keys": sorted(getattr(task, "runtime", {}).keys()),
            }
        )

    workflows = []
    if document.workflow is not None:
        body_counts = Counter(
            type(item).__name__.lower() for item in getattr(document.workflow, "body", [])
        )
        workflows.append(
            {
                "name": document.workflow.name,
                **_source_position(document.workflow),
                "inputs": [
                    _declaration_payload(item)
                    for item in (getattr(document.workflow, "inputs", None) or [])
                ],
                "outputs": [
                    _declaration_payload(item)
                    for item in (getattr(document.workflow, "outputs", None) or [])
                ],
                "structure": {
                    "call_count": body_counts.get("call", 0),
                    "scatter_count": body_counts.get("scatter", 0),
                    "conditional_count": body_counts.get("conditional", 0),
                },
            }
        )

    imports = [
        {
            "uri": item.uri,
            "namespace": item.namespace,
            **_source_position(item),
        }
        for item in document.imports
    ]
    return {
        "status": "invalid" if diagnostics else "valid",
        "parsed": True,
        "wdl_version": str(document.wdl_version),
        "summary": {
            "task_count": len(tasks),
            "workflow_count": len(workflows),
            "import_count": len(imports),
            "error_count": len(diagnostics),
        },
        "imports": imports,
        "tasks": tasks,
        "workflows": workflows,
        "diagnostics": diagnostics,
    }


def _unified_diff(before: str, after: str, filename: str) -> str:
    normalized_before = _normalize_newlines(before)
    normalized_after = _normalize_newlines(after)
    if normalized_before == normalized_after:
        return ""
    lines = difflib.unified_diff(
        normalized_before.splitlines(),
        normalized_after.splitlines(),
        fromfile=f"{filename} (before)",
        tofile=f"{filename} (after)",
        lineterm="",
    )
    return "\n".join(lines) + "\n"


def _bundle_diff(before: dict[str, str], after: dict[str, str]) -> str:
    chunks = []
    for path in sorted(set(before) | set(after)):
        diff = _unified_diff(before.get(path, ""), after.get(path, ""), path)
        if diff:
            chunks.append(diff.rstrip())
    return "\n\n".join(chunks) + ("\n" if chunks else "")


def _revision_files(revision: WDLSourceRevision | None) -> tuple[dict[str, str], str]:
    if revision is None:
        return {}, ""
    source_files = list(revision.files.all())
    if not source_files:
        return {revision.asset.source_filename: revision.content}, revision.asset.source_filename
    entry = next((item.path for item in source_files if item.is_entry), source_files[0].path)
    return {item.path: item.content for item in source_files}, entry


def _save_source_files(
    revision: WDLSourceRevision,
    files: dict[str, str],
    entrypoint: str,
    analysis: dict,
) -> None:
    file_analysis = {item["path"]: item for item in analysis.get("files", [])}
    WDLSourceFile.objects.bulk_create(
        [
            WDLSourceFile(
                revision=revision,
                path=path,
                content=content,
                digest=digest(content),
                is_entry=path == entrypoint,
                analysis=file_analysis.get(path, {}),
            )
            for path, content in sorted(files.items())
        ]
    )


def _json_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed
    return value


def _request_reference_specs(
    request,
    *,
    fallback_revision: WDLSourceRevision | None = None,
) -> list[PackageReferenceSpec]:
    raw = request.data.get("package_references")
    return parse_reference_specs(
        _json_list(raw) if raw is not None else None,
        fallback_revision=fallback_revision,
    )


def _request_bundle(request, *, fallback_revision: WDLSourceRevision | None = None):
    entrypoint = str(request.data.get("entrypoint") or request.data.get("filename") or "").strip()
    archive = request.FILES.get("archive")
    if archive is not None:
        if str(getattr(archive, "name", "")).lower().endswith(".wdl"):
            try:
                content = archive.read(MAX_WDL_CONTENT_LENGTH + 1).decode("utf-8")
            except UnicodeDecodeError as error:
                raise WDLPackageError(
                    "WDL_PACKAGE_ENCODING_INVALID", "The uploaded WDL is not UTF-8."
                ) from error
            if len(content) > MAX_WDL_CONTENT_LENGTH:
                raise WDLPackageError(
                    "WDL_CONTENT_INVALID",
                    "content exceeds the 2,000,000 character limit.",
                )
            path = entrypoint or Path(archive.name).name
            return normalize_bundle_files([{"path": path, "content": content}], path)
        return read_wdl_archive(archive, entrypoint)
    raw_files = _json_list(request.data.get("files"))
    if raw_files is not None:
        return normalize_bundle_files(raw_files, entrypoint)
    content = request.data.get("content")
    if content is not None:
        error = _content_error(content)
        if error:
            raise WDLPackageError("WDL_CONTENT_INVALID", error)
        filename = entrypoint or (
            fallback_revision.asset.source_filename if fallback_revision else "workflow.wdl"
        )
        return normalize_bundle_files([{"path": filename, "content": content}], filename)
    if fallback_revision is not None:
        return _revision_files(fallback_revision)
    raise WDLPackageError("WDL_CONTENT_INVALID", "A WDL document or package is required.")


def _package_error_response(error: WDLPackageError, request_id: str) -> Response:
    return _with_request_id(
        Response(
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    **({"details": error.details} if error.details else {}),
                }
            },
            status=status.HTTP_400_BAD_REQUEST,
        ),
        request_id,
    )


def _brace_delta(line: str) -> tuple[int, int]:
    opens = 0
    closes = 0
    quote = None
    escaped = False
    for character in line:
        if escaped:
            escaped = False
            continue
        if quote and character == "\\":
            escaped = True
            continue
        if character in {'"', "'"}:
            quote = None if quote == character else character if quote is None else quote
            continue
        if quote is not None:
            continue
        if character == "#":
            break
        if character == "{":
            opens += 1
        elif character == "}":
            closes += 1
    return opens, closes


def _format_legacy_wdl(content: str, filename: str) -> str:
    normalized_content = _normalize_newlines(content)
    document = WDL.parse_document(normalized_content, uri=filename)
    command_lines: dict[int, tuple[str, int]] = {}
    source_lines = normalized_content.splitlines()
    for task in document.tasks:
        position = getattr(task.command, "pos", None)
        if position is not None:
            body_lines = source_lines[position.line : position.end_line - 1]
            body_indents = [
                len(line) - len(line.lstrip(" \t"))
                for line in body_lines
                if line.strip()
            ]
            common_indent = min(body_indents, default=0)
            command_lines[position.line] = ("delimiter", 0)
            command_lines[position.end_line] = ("delimiter", 0)
            for line_number in range(position.line + 1, position.end_line):
                command_lines[line_number] = ("body", common_indent)

    formatted = []
    indent = 0
    continuation_indent = 0
    blank_count = 0
    for line_number, raw_line in enumerate(source_lines, start=1):
        command_line = command_lines.get(line_number)
        if command_line and command_line[0] == "body":
            common_indent = command_line[1]
            if raw_line.strip():
                formatted.append(f"{'  ' * (indent + 1)}{raw_line[common_indent:]}")
            else:
                formatted.append("")
            blank_count = 0
            continue
        stripped = raw_line.strip()
        if not stripped:
            blank_count += 1
            if blank_count <= 1 and formatted:
                formatted.append("")
            continue
        blank_count = 0
        if stripped.startswith("}") and continuation_indent:
            indent = max(0, indent - continuation_indent)
            continuation_indent = 0
        if command_line and command_line[0] == "delimiter":
            formatted.append(f"{'  ' * indent}{stripped}")
            continue
        leading_closes = len(stripped) - len(stripped.lstrip("}"))
        line_indent = max(0, indent - leading_closes)
        formatted.append(f"{'  ' * line_indent}{stripped}")
        opens, closes = _brace_delta(stripped)
        indent = max(0, indent + opens - closes)
        if stripped.endswith(":") and not opens and not closes:
            indent += 1
            continuation_indent += 1

    result = "\n".join(formatted).rstrip() + "\n"
    WDL.parse_document(result, uri=filename)
    return result


def _format_wdl_with_sprocket(content: str, filename: str) -> str:
    config_path = Path(settings.SPROCKET_FORMAT_CONFIG)
    if not config_path.is_file():
        raise WDLFormatterUnavailable("Sprocket formatter configuration is missing.")

    with tempfile.TemporaryDirectory(prefix="bioworkflow-wdl-format-") as temp_dir:
        source_path = Path(temp_dir) / "source.wdl"
        source_path.write_text(content, encoding="utf-8", newline="\n")
        command = [
            settings.SPROCKET_BINARY,
            "format",
            "--skip-config-search",
            "--config",
            str(config_path),
            "view",
            str(source_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                encoding="utf-8",
                timeout=settings.SPROCKET_FORMAT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as error:
            raise WDLFormatterUnavailable(
                "Sprocket formatter executable is unavailable."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise WDLFormatterUnavailable(
                "Sprocket formatter timed out."
            ) from error
        except OSError as error:
            raise WDLFormatterUnavailable(
                "Sprocket formatter could not be started."
            ) from error

        if completed.returncode != 0:
            message = completed.stderr.strip() or "Sprocket rejected the WDL document."
            message = message.replace(str(source_path), filename)
            raise WDLFormatterRejected(message[:2000])
        if not completed.stdout.strip():
            raise WDLFormatterRejected("Sprocket returned an empty WDL document.")
        return _normalize_newlines(completed.stdout)


def format_wdl(content: str, filename: str) -> str:
    normalized_content = _normalize_newlines(content)
    document = WDL.parse_document(normalized_content, uri=filename)
    if document.wdl_version is None:
        return _format_legacy_wdl(normalized_content, filename)

    formatted = _format_wdl_with_sprocket(normalized_content, filename)
    WDL.parse_document(formatted, uri=filename)
    return formatted


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
        seen.add(key)
        names.append(name)
    return names


def _resolve_tags(names: list[str]) -> list[WDLTag]:
    tags = []
    for name in names:
        tag = WDLTag.objects.filter(name__iexact=name).first()
        if tag is None:
            tag = WDLTag.objects.create(name=name)
        tags.append(tag)
    return tags


def _set_tags(asset: WDLAsset, names: list[str]) -> list[str]:
    tags = _resolve_tags(names)
    asset.tags.set(tags)
    return [tag.name for tag in tags]


def _tag_payload(tag: WDLTag) -> dict:
    asset_count = getattr(tag, "asset_count", None)
    if asset_count is None:
        asset_count = tag.wdl_assets.count()
    return {
        "id": tag.id,
        "name": tag.name,
        "asset_count": asset_count,
    }


def _unique_slug(name: str, requested_slug: str | None = None) -> str:
    base = slugify(requested_slug or name)[:112] or f"wdl-{uuid.uuid4().hex[:8]}"
    candidate = base
    suffix = 2
    while WDLAsset.objects.filter(slug=candidate).exists():
        candidate = f"{base[:112]}-{suffix}"
        suffix += 1
    return candidate


def _revision_payload(
    revision: WDLSourceRevision, *, include_content: bool = False
) -> dict:
    local_source_files = list(revision.files.all())
    specifications = reference_specs_for_revision(revision)
    reference_payloads = [
        package_reference_payload(specification) for specification in specifications
    ]
    if local_source_files:
        entrypoint = next(
            (item.path for item in local_source_files if item.is_entry),
            local_source_files[0].path,
        )
        local_files = {item.path: item.content for item in local_source_files}
        local_by_path = {item.path: item for item in local_source_files}
    else:
        entrypoint = revision.asset.source_filename
        local_files = {entrypoint: revision.content}
        local_by_path = {}
    effective_files, package_origins = effective_package_files(
        local_files, specifications
    )
    files = []
    for path, content in sorted(effective_files.items()):
        local_file = local_by_path.get(path)
        package_origin = package_origins.get(path)
        if package_origin:
            specification, package_file = package_origin
            package_version = specification.package_version
            file_payload = {
                "path": path,
                "digest": package_file.digest,
                "is_entry": False,
                "analysis": package_file.analysis,
                "origin": "package",
                "read_only": True,
                "package_reference": {
                    "package_slug": package_version.package.slug,
                    "package_name": package_version.package.name,
                    "version": package_version.version,
                    "digest": specification.digest,
                    "mount_prefix": specification.mount_prefix,
                    "package_file_path": package_file.path,
                },
            }
        else:
            file_payload = {
                "path": path,
                "digest": local_file.digest if local_file else revision.digest,
                "is_entry": local_file.is_entry if local_file else path == entrypoint,
                "analysis": local_file.analysis if local_file else {},
                "origin": "asset",
                "read_only": False,
            }
        if include_content:
            file_payload["content"] = content
        files.append(file_payload)
    payload = {
        "version": revision.version,
        "operation": revision.operation,
        "digest": revision.digest,
        "diff": revision.diff,
        "note": revision.note,
        "actor": revision.actor,
        "analysis": revision.analysis,
        "entrypoint": entrypoint,
        "files": files,
        "package_references": reference_payloads,
        "created_at": revision.created_at.isoformat(),
    }
    if include_content:
        payload["content"] = revision.content
    return payload


def _audit_payload(event: WDLAuditEvent) -> dict:
    return {
        "id": event.id,
        "action": event.action,
        "actor": event.actor,
        "note": event.note,
        "changes": event.changes,
        "diff": event.diff,
        "revision": event.revision.version if event.revision_id else None,
        "created_at": event.created_at.isoformat(),
    }


def _asset_payload(asset: WDLAsset, *, include_detail: bool = False) -> dict:
    latest = asset.source_revisions.first()
    latest_payload = (
        _revision_payload(latest, include_content=include_detail) if latest else None
    )
    payload = {
        "slug": asset.slug,
        "name": asset.name,
        "description": asset.description,
        "source_filename": asset.source_filename,
        "source_repository": asset.source_repository,
        "source_revision": asset.source_revision,
        "lifecycle": asset.lifecycle,
        "tags": [tag.name for tag in asset.tags.all()],
        "created_by": asset.created_by,
        "created_at": asset.created_at.isoformat(),
        "updated_at": asset.updated_at.isoformat(),
        "revision_count": asset.source_revisions.count(),
        "file_count": len(latest_payload["files"]) if latest_payload else 0,
        "current_revision": latest_payload,
    }
    if include_detail:
        payload["revisions"] = [
            _revision_payload(item) for item in asset.source_revisions.all()[:100]
        ]
        payload["audit_events"] = [
            _audit_payload(item)
            for item in asset.audit_events.select_related("revision").all()[:200]
        ]
    return payload


def _content_error(content) -> str | None:
    if not isinstance(content, str) or not content.strip():
        return "content must be a non-empty WDL document."
    if len(content) > MAX_WDL_CONTENT_LENGTH:
        return "content exceeds the 2,000,000 character limit."
    return None


@api_view(["GET", "POST"])
def wdl_assets(request):
    request_id = _request_id(request)
    if request.method == "GET":
        assets = WDLAsset.objects.prefetch_related("tags", *ASSET_REVISION_PREFETCHES)
        query = request.query_params.get("q", "").strip()
        if query:
            assets = assets.filter(
                Q(name__icontains=query)
                | Q(slug__icontains=query)
                | Q(description__icontains=query)
                | Q(source_filename__icontains=query)
            )
        lifecycle = request.query_params.get("lifecycle", "").strip()
        if lifecycle:
            assets = assets.filter(lifecycle=lifecycle)
        tags = [item for item in request.query_params.getlist("tag") if item]
        if tags:
            assets = assets.filter(tags__name__in=tags).distinct()
        return _with_request_id(
            Response({"results": [_asset_payload(item) for item in assets]}),
            request_id,
        )

    try:
        files, entrypoint = _request_bundle(request)
        reference_specs = _request_reference_specs(request)
        effective_files, _ = effective_package_files(files, reference_specs)
    except WDLPackageError as error:
        return _package_error_response(error, request_id)
    name = str(request.data.get("name") or "").strip()
    filename = entrypoint
    tags = _tag_names(_json_list(request.data.get("tags", [])))
    lifecycle = request.data.get("lifecycle", WDLAsset.Lifecycle.ACTIVE)
    if not name or len(name) > 256 or not filename or len(filename) > 512:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_ASSET_METADATA_INVALID",
                        "message": "name and filename are required.",
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    if tags is None or lifecycle not in WDLAsset.Lifecycle.values:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_ASSET_METADATA_INVALID",
                        "message": "tags or lifecycle is invalid.",
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )

    actor = _actor(request)
    note = str(request.data.get("note") or "").strip()
    analysis = analyze_wdl_bundle(effective_files, entrypoint)
    content = files[entrypoint]
    with transaction.atomic():
        asset = WDLAsset.objects.create(
            slug=_unique_slug(name, request.data.get("slug")),
            name=name,
            description=str(request.data.get("description") or "").strip(),
            source_filename=filename,
            source_repository=str(request.data.get("source_repository") or "").strip()[:512],
            source_revision=str(request.data.get("source_revision") or "").strip()[:128],
            lifecycle=lifecycle,
            created_by=actor,
        )
        tags = _set_tags(asset, tags)
        revision = WDLSourceRevision.objects.create(
            asset=asset,
            version=1,
            operation=WDLSourceRevision.Operation.IMPORT,
            content=content,
            digest=_digest(content),
            note=note,
            actor=actor,
            analysis=analysis,
        )
        _save_source_files(revision, files, entrypoint, analysis)
        persist_reference_specs(revision, reference_specs)
        WDLAuditEvent.objects.create(
            asset=asset,
            revision=revision,
            action="import",
            actor=actor,
            note=note,
            changes={
                "tags": {"before": [], "after": tags},
                "package": {
                    "entrypoint": entrypoint,
                    "file_count": len(effective_files),
                    "references": [
                        package_reference_payload(item) for item in reference_specs
                    ],
                },
            },
        )
    return _with_request_id(
        Response(_asset_payload(asset, include_detail=True), status=status.HTTP_201_CREATED),
        request_id,
    )


@api_view(["GET", "PATCH"])
def wdl_asset_detail(request, slug: str):
    request_id = _request_id(request)
    asset = (
        WDLAsset.objects.prefetch_related("tags", *ASSET_REVISION_PREFETCHES)
        .filter(slug=slug)
        .first()
    )
    if asset is None:
        return _with_request_id(
            Response(
                {"error": {"code": "WDL_ASSET_NOT_FOUND", "message": "WDL asset not found."}},
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    if request.method == "GET":
        return _with_request_id(
            Response(_asset_payload(asset, include_detail=True)), request_id
        )

    changes = {}
    for field, max_length in (
        ("name", 256),
        ("description", None),
        ("source_filename", 512),
    ):
        if field not in request.data:
            continue
        value = str(request.data[field] or "").strip()
        if field != "description" and (not value or len(value) > max_length):
            return _with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WDL_ASSET_METADATA_INVALID",
                            "message": f"{field} is invalid.",
                        }
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                ),
                request_id,
            )
        if value != getattr(asset, field):
            changes[field] = {"before": getattr(asset, field), "after": value}
            setattr(asset, field, value)

    if "lifecycle" in request.data:
        lifecycle = request.data["lifecycle"]
        if lifecycle not in WDLAsset.Lifecycle.values:
            return _with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WDL_ASSET_METADATA_INVALID",
                            "message": "lifecycle is invalid.",
                        }
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                ),
                request_id,
            )
        if lifecycle != asset.lifecycle:
            changes["lifecycle"] = {"before": asset.lifecycle, "after": lifecycle}
            asset.lifecycle = lifecycle

    if "tags" in request.data:
        tags = _tag_names(request.data["tags"])
        if tags is None:
            return _with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WDL_ASSET_METADATA_INVALID",
                            "message": "tags must be an array of names.",
                        }
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                ),
                request_id,
            )
        current_tags = [tag.name for tag in asset.tags.all()]
        if {tag.casefold() for tag in current_tags} != {
            tag.casefold() for tag in tags
        }:
            canonical_tags = _set_tags(asset, tags)
            changes["tags"] = {"before": current_tags, "after": canonical_tags}

    if changes:
        asset.save()
        WDLAuditEvent.objects.create(
            asset=asset,
            action="metadata_update",
            actor=_actor(request),
            note=str(request.data.get("note") or "").strip(),
            changes=changes,
        )
    return _with_request_id(
        Response(_asset_payload(asset, include_detail=True)), request_id
    )


@api_view(["GET", "POST"])
def wdl_asset_revisions(request, slug: str):
    request_id = _request_id(request)
    asset = (
        WDLAsset.objects.prefetch_related(*ASSET_REVISION_PREFETCHES)
        .filter(slug=slug)
        .first()
    )
    if asset is None:
        return _with_request_id(
            Response(
                {"error": {"code": "WDL_ASSET_NOT_FOUND", "message": "WDL asset not found."}},
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    if request.method == "GET":
        return _with_request_id(
            Response(
                {
                    "results": [
                        _revision_payload(item)
                        for item in asset.source_revisions.all()[:100]
                    ]
                }
            ),
            request_id,
        )

    operation = request.data.get("operation", WDLSourceRevision.Operation.EDIT)
    if operation not in {
        WDLSourceRevision.Operation.EDIT,
        WDLSourceRevision.Operation.FORMAT,
    }:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_REVISION_INVALID",
                        "message": "operation must be edit or format.",
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    latest_snapshot = asset.source_revisions.first()
    try:
        files, entrypoint = _request_bundle(request, fallback_revision=latest_snapshot)
        reference_specs = _request_reference_specs(
            request, fallback_revision=latest_snapshot
        )
        effective_files, _ = effective_package_files(files, reference_specs)
    except WDLPackageError as error:
        return _package_error_response(error, request_id)

    actor = _actor(request)
    note = str(request.data.get("note") or "").strip()
    with transaction.atomic():
        locked = WDLAsset.objects.select_for_update().get(pk=asset.pk)
        latest = locked.source_revisions.prefetch_related(
            "files", *REVISION_REFERENCE_PREFETCHES
        ).first()
        before_files, _ = _revision_files(latest)
        before_reference_specs = reference_specs_for_revision(latest)
        before_reference_keys = {
            reference_spec_key(item) for item in before_reference_specs
        }
        reference_keys = {reference_spec_key(item) for item in reference_specs}
        if (
            latest is not None
            and before_files == files
            and before_reference_keys == reference_keys
        ):
            return _with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WDL_REVISION_UNCHANGED",
                            "message": "The WDL content has not changed.",
                        }
                    },
                    status=status.HTTP_409_CONFLICT,
                ),
                request_id,
            )
        version = (latest.version if latest else 0) + 1
        diff = _bundle_diff(before_files, files)
        analysis = analyze_wdl_bundle(effective_files, entrypoint)
        content = files[entrypoint]
        revision = WDLSourceRevision.objects.create(
            asset=locked,
            version=version,
            operation=operation,
            content=content,
            digest=_digest(content),
            diff=diff,
            note=note,
            actor=actor,
            analysis=analysis,
        )
        _save_source_files(revision, files, entrypoint, analysis)
        persist_reference_specs(revision, reference_specs)
        locked.source_filename = entrypoint
        locked.save(update_fields=["source_filename", "updated_at"])
        WDLAuditEvent.objects.create(
            asset=locked,
            revision=revision,
            action=operation,
            actor=actor,
            note=note,
            diff=diff,
            changes={
                "revision": {
                    "before": latest.version if latest else None,
                    "after": version,
                },
                "package": {
                    "entrypoint": entrypoint,
                    "file_count": len(effective_files),
                    "changed_files": sorted(
                        path
                        for path in set(before_files) | set(files)
                        if before_files.get(path) != files.get(path)
                    ),
                    "references": {
                        "before": [
                            package_reference_payload(item)
                            for item in before_reference_specs
                        ],
                        "after": [
                            package_reference_payload(item)
                            for item in reference_specs
                        ],
                    },
                },
            },
        )
    return _with_request_id(
        Response(
            _revision_payload(revision, include_content=True),
            status=status.HTTP_201_CREATED,
        ),
        request_id,
    )


@api_view(["GET"])
def wdl_asset_revision_detail(request, slug: str, version: int):
    request_id = _request_id(request)
    revision = (
        WDLSourceRevision.objects.select_related("asset")
        .prefetch_related("files", *REVISION_REFERENCE_PREFETCHES)
        .filter(asset__slug=slug, version=version)
        .first()
    )
    if revision is None:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_SOURCE_REVISION_NOT_FOUND",
                        "message": "WDL source revision not found.",
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    return _with_request_id(
        Response(_revision_payload(revision, include_content=True)), request_id
    )


@api_view(["POST"])
def format_wdl_asset(request, slug: str):
    request_id = _request_id(request)
    asset = WDLAsset.objects.filter(slug=slug).first()
    if asset is None:
        return _with_request_id(
            Response(
                {"error": {"code": "WDL_ASSET_NOT_FOUND", "message": "WDL asset not found."}},
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    content = request.data.get("content")
    error = _content_error(content)
    if error:
        return _with_request_id(
            Response(
                {"error": {"code": "WDL_CONTENT_INVALID", "message": error}},
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    filename = str(request.data.get("filename") or asset.source_filename).strip()
    try:
        formatted = format_wdl(content, filename)
    except WDLFormatterUnavailable as format_error:
        return _with_request_id(
            Response(
                {
                    "status": "unavailable",
                    "error": {
                        "code": "WDL_FORMATTER_UNAVAILABLE",
                        "message": str(format_error),
                    },
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            ),
            request_id,
        )
    except Exception as format_error:
        analysis = analyze_wdl(content, filename)
        diagnostics = analysis.get("diagnostics") or []
        message = str(format_error).strip()
        if not message and diagnostics:
            message = str(diagnostics[0].get("message") or "").strip()
        message = message or type(format_error).__name__
        return _with_request_id(
            Response(
                {
                    "status": "rejected",
                    "error": {
                        "code": "WDL_FORMAT_REQUIRES_VALID_SYNTAX",
                        "message": message,
                    },
                    "analysis": analysis,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
            request_id,
        )
    return _with_request_id(
        Response(
            {
                "content": formatted,
                "changed": formatted != content,
                "diff": _unified_diff(content, formatted, filename),
                "analysis": analyze_wdl(formatted, filename),
            }
        ),
        request_id,
    )


@api_view(["GET", "POST"])
def export_wdl_asset(request, slug: str):
    asset = WDLAsset.objects.filter(slug=slug).first()
    if asset is None:
        return Response(
            {"error": {"code": "WDL_ASSET_NOT_FOUND", "message": "WDL asset not found."}},
            status=status.HTTP_404_NOT_FOUND,
        )
    requested_version = request.query_params.get("version") or request.data.get("version")
    revisions = asset.source_revisions.prefetch_related(
        "files", *REVISION_REFERENCE_PREFETCHES
    )
    revision = revisions.filter(version=requested_version).first() if requested_version else revisions.first()
    if revision is None:
        return Response(
            {"error": {"code": "WDL_SOURCE_REVISION_NOT_FOUND", "message": "WDL source revision not found."}},
            status=status.HTTP_404_NOT_FOUND,
        )
    try:
        local_files, entrypoint = (
            _request_bundle(request, fallback_revision=revision)
            if request.method == "POST"
            else _revision_files(revision)
        )
        reference_specs = (
            _request_reference_specs(request, fallback_revision=revision)
            if request.method == "POST"
            else reference_specs_for_revision(revision)
        )
        files, _ = effective_package_files(local_files, reference_specs)
    except WDLPackageError as error:
        return _package_error_response(error, _request_id(request))
    if len(files) == 1:
        response = HttpResponse(files[entrypoint], content_type="application/wdl; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{Path(entrypoint).name}"'
        return response
    archive = build_wdl_archive(
        files,
        entrypoint,
        asset={
            "slug": asset.slug,
            "name": asset.name,
            "version": revision.version,
            "packageReferences": [
                package_reference_payload(item) for item in reference_specs
            ],
        },
    )
    response = HttpResponse(archive, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{asset.slug}-v{revision.version}.zip"'
    return response


@api_view(["POST"])
def import_wdl_task(request, slug: str):
    request_id = _request_id(request)
    asset = WDLAsset.objects.filter(slug=slug).first()
    if asset is None:
        return _with_request_id(
            Response(
                {"error": {"code": "WDL_ASSET_NOT_FOUND", "message": "WDL asset not found."}},
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    version = request.data.get("version")
    revisions = asset.source_revisions.prefetch_related(
        "files", *REVISION_REFERENCE_PREFETCHES
    )
    revision = revisions.filter(version=version).first() if version else revisions.first()
    file_path = str(request.data.get("file_path") or "").strip()
    task_name = str(request.data.get("task_name") or "").strip()
    source_file = revision.files.filter(path=file_path).first() if revision else None
    if revision is not None and source_file is None and file_path:
        try:
            local_files, _ = _revision_files(revision)
            effective_files, _ = effective_package_files(
                local_files, reference_specs_for_revision(revision)
            )
        except WDLPackageError as error:
            return _package_error_response(error, request_id)
        if file_path in effective_files:
            source_file = ResolvedWDLTaskSource(
                path=file_path,
                content=effective_files[file_path],
            )
    if revision is None or source_file is None or not task_name:
        return _with_request_id(
            Response(
                {"error": {"code": "WDL_TASK_NOT_FOUND", "message": "Select a task from a saved WDL revision."}},
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    actor = _actor(request)
    try:
        document, created, warnings = import_task_as_tool_draft(
            asset=asset,
            revision=revision,
            source_file=source_file,
            task_name=task_name,
            actor=actor,
            tool_id=str(request.data.get("tool_id") or "").strip(),
            replace=bool(request.data.get("replace", False)),
        )
    except WDLTaskImportError as error:
        return _with_request_id(
            Response(
                {"error": {"code": error.code, "message": error.message}},
                status=status.HTTP_409_CONFLICT if error.code == "TOOL_DRAFT_EXISTS" else status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    WDLAuditEvent.objects.create(
        asset=asset,
        revision=revision,
        action="tool_import",
        actor=actor,
        changes={
            "tool_id": document.tool_id,
            "file_path": source_file.path,
            "task_name": task_name,
            "created": created,
        },
    )
    return _with_request_id(
        Response(
            {
                "tool_id": document.tool_id,
                "created": created,
                "warnings": warnings,
                "validation": document.validation,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        ),
        request_id,
    )


@api_view(["GET", "POST"])
def wdl_tags(request):
    request_id = _request_id(request)
    if request.method == "GET":
        tags = WDLTag.objects.annotate(asset_count=Count("wdl_assets")).order_by(
            "-asset_count", "name"
        )
        return _with_request_id(
            Response(
                {
                    "results": [
                        _tag_payload(tag) for tag in tags
                    ]
                }
            ),
            request_id,
        )
    name = str(request.data.get("name") or "").strip()
    if not name or len(name) > 64:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_TAG_INVALID",
                        "message": "name is required and must be at most 64 characters.",
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    tag = WDLTag.objects.filter(name__iexact=name).first()
    created = tag is None
    if tag is None:
        tag = WDLTag.objects.create(name=name)
    return _with_request_id(
        Response(
            _tag_payload(tag),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        ),
        request_id,
    )


@api_view(["PATCH", "DELETE"])
@transaction.atomic
def wdl_tag_detail(request, tag_id: int):
    request_id = _request_id(request)
    tag = WDLTag.objects.select_for_update().filter(id=tag_id).first()
    if tag is None:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_TAG_NOT_FOUND",
                        "message": "WDL tag not found.",
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )

    asset_count = tag.wdl_assets.count()
    if request.method == "DELETE":
        if asset_count:
            return _with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WDL_TAG_IN_USE",
                            "message": "Used WDL tags cannot be deleted.",
                        },
                        "asset_count": asset_count,
                    },
                    status=status.HTTP_409_CONFLICT,
                ),
                request_id,
            )
        tag.delete()
        return _with_request_id(
            Response(status=status.HTTP_204_NO_CONTENT),
            request_id,
        )

    name = str(request.data.get("name") or "").strip()
    if not name or len(name) > 64:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_TAG_INVALID",
                        "message": "name is required and must be at most 64 characters.",
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    if (
        WDLTag.objects.filter(name__iexact=name)
        .exclude(id=tag.id)
        .exists()
    ):
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_TAG_CONFLICT",
                        "message": "A WDL tag with this name already exists.",
                    }
                },
                status=status.HTTP_409_CONFLICT,
            ),
            request_id,
        )
    if name == tag.name:
        return _with_request_id(Response(_tag_payload(tag)), request_id)

    assets = list(tag.wdl_assets.prefetch_related("tags"))
    previous_name = tag.name
    previous_tags = {
        asset.id: [item.name for item in asset.tags.all()]
        for asset in assets
    }
    tag.name = name
    tag.save(update_fields=["name"])
    for asset in assets:
        before = previous_tags[asset.id]
        after = [name if item == previous_name else item for item in before]
        asset.save(update_fields=["updated_at"])
        WDLAuditEvent.objects.create(
            asset=asset,
            action="metadata_update",
            actor=_actor(request),
            note=f"重命名标签 {previous_name} → {name}",
            changes={"tags": {"before": before, "after": after}},
        )

    return _with_request_id(Response(_tag_payload(tag)), request_id)

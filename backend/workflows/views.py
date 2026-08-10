from __future__ import annotations

import json
import hashlib
import shutil
import uuid
from pathlib import Path

from django.db import connection, transaction
from django.db.models import Max
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from compiler_core import (
    canonical_digest,
    compile_workflow,
    validate_tool_spec,
    validate_workflow_graph,
)
from compiler_core.validation import semantic_digest
from compiler_core.compiler import validate_wdl
from .models import (
    CompilationRecord,
    ToolDocument,
    ToolVersion,
    WDLRevision,
    WorkflowDocument,
    WorkflowVersion,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = {
    "tool-spec": ("tool-spec.schema.json", "1.0.0"),
    "workflow-graph": ("workflow-graph.schema.json", "1.0.0"),
    "compiler-ir": ("compiler-ir.schema.json", "1.0.0"),
    "validation-report": ("validation-report.schema.json", "1.0.0"),
    "error-catalog": ("error-catalog.json", "1.0.0"),
}


def _dependency_validation(graph: dict, errors: list[dict]) -> dict:
    diagnostics = [
        {
            "code": item["code"],
            "stage": "resolution",
            "severity": "error",
            "message": item["message"],
            **({"location": {"node_id": item["node_id"]}} if item.get("node_id") else {}),
        }
        for item in errors
    ]
    return {
        "report_version": "1.0.0",
        "status": "invalid",
        "validation_id": f"val_{uuid.uuid4().hex}",
        "summary": {"error_count": len(diagnostics), "warning_count": 0},
        "diagnostics": diagnostics,
        "source": {
            "kind": "workflow_graph",
            "id": graph.get("id", "unknown"),
            "digest": semantic_digest(graph),
        },
    }


def _resolve_subworkflow_specs(graph: dict) -> tuple[list[dict], list[dict]]:
    """Resolve exact immutable subworkflow snapshots and compile their WDL dependencies."""
    resolved: dict[tuple[str, int, str], dict] = {}
    errors: list[dict] = []

    def visit(node: dict, ancestors: tuple[tuple[str, int], ...]) -> None:
        ref = node.get("subworkflow_ref") or {}
        slug, version, digest = ref.get("slug"), ref.get("version"), ref.get("digest")
        identity = (slug, version)
        if identity in ancestors:
            errors.append(
                {
                    "code": "WG023",
                    "node_id": node.get("id"),
                    "message": f"Recursive subworkflow dependency detected at {slug}@{version}.",
                }
            )
            return
        snapshot = WorkflowVersion.objects.filter(
            workflow__slug=slug,
            version=version,
            kind=WorkflowDocument.Kind.SUBWORKFLOW,
            semantic_digest=digest,
        ).first()
        if snapshot is None:
            errors.append(
                {
                    "code": "WG021",
                    "node_id": node.get("id"),
                    "message": f"Published subworkflow {slug}@{version} with the pinned digest was not found.",
                }
            )
            return
        if node.get("interface_contract") != snapshot.interface_contract:
            errors.append(
                {
                    "code": "WG022",
                    "node_id": node.get("id"),
                    "message": f"Interface snapshot for {slug}@{version} is stale or modified.",
                }
            )
            return
        key = (slug, version, digest)
        if key in resolved:
            return
        descendants = [
            child
            for child in snapshot.workflow_graph.get("nodes", [])
            if child.get("type") == "subworkflow"
        ]
        for child in descendants:
            visit(child, (*ancestors, identity))
        if errors:
            return
        dependency_specs = list(resolved.values())
        validation, artifacts = compile_workflow(
            snapshot.workflow_graph, snapshot.tool_specs, dependency_specs
        )
        if validation["status"] != "valid":
            errors.append(
                {
                    "code": "WG024",
                    "node_id": node.get("id"),
                    "message": f"Published subworkflow {slug}@{version} no longer compiles.",
                }
            )
            return
        wdl = next(item["content"] for item in artifacts if item["name"] == "workflow.wdl")
        resolved[key] = {
            "slug": slug,
            "version": version,
            "semantic_digest": digest,
            "interface_contract": snapshot.interface_contract,
            "workflow_name": snapshot.workflow_graph["id"],
            "namespace": f"{slug}_v{version}",
            "artifact_path": f"{slug}.v{version}.wdl",
            "wdl": wdl,
        }

    for graph_node in graph.get("nodes", []):
        if graph_node.get("type") == "subworkflow":
            visit(graph_node, ())
    return list(resolved.values()), errors


def _request_id(request) -> str:
    candidate = request.headers.get("X-Request-ID", "")
    if candidate and len(candidate) <= 128 and candidate.replace("-", "_").isalnum():
        return candidate
    return f"req_{uuid.uuid4().hex}"


def _with_request_id(response: Response, request_id: str) -> Response:
    response["X-Request-ID"] = request_id
    return response


@api_view(["GET"])
def health(request):
    request_id = _request_id(request)
    database = "available"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database = "unavailable"
    body = {
        "status": "ok" if database == "available" else "degraded",
        "service": "bioworkflow-compiler-api",
        "api_version": "v1",
        "compiler_contract": "phase1",
        "dependencies": {
            "database": database,
            "miniwdl": "available" if shutil.which("miniwdl") else "not_configured",
        },
    }
    return _with_request_id(Response(body), request_id)


@api_view(["GET"])
def contracts(request, contract_name: str | None = None):
    request_id = _request_id(request)
    if contract_name is None:
        response = Response(
            {
                "contracts": [
                    {"name": name, "version": version}
                    for name, (_, version) in CONTRACTS.items()
                ]
            }
        )
    elif contract_name not in CONTRACTS:
        response = Response(
            {"error": {"code": "CONTRACT_NOT_FOUND", "message": "Unknown contract.", "request_id": request_id}},
            status=status.HTTP_404_NOT_FOUND,
        )
    else:
        response = Response(json.loads((ROOT / "schemas" / CONTRACTS[contract_name][0]).read_text(encoding="utf-8")))
    return _with_request_id(response, request_id)


@api_view(["POST"])
def validate_tool(request):
    request_id = _request_id(request)
    tool = request.data.get("tool_spec")
    if not isinstance(tool, dict):
        return _with_request_id(
            Response(
                {"error": {"code": "REQUEST_INVALID", "message": "tool_spec is required.", "request_id": request_id}},
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    validation = validate_tool_spec(tool)
    return _with_request_id(
        Response({"status": "completed", "validation": validation, "normalized": {"digest": canonical_digest(tool)}}),
        request_id,
    )


@api_view(["POST"])
def validate_graph(request):
    request_id = _request_id(request)
    graph = request.data.get("workflow_graph")
    tools = request.data.get("tool_specs", [])
    if not isinstance(graph, dict) or not isinstance(tools, list):
        return _with_request_id(
            Response(
                {"error": {"code": "REQUEST_INVALID", "message": "workflow_graph and tool_specs are required.", "request_id": request_id}},
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    subworkflows, dependency_errors = _resolve_subworkflow_specs(graph)
    if dependency_errors:
        validation = _dependency_validation(graph, dependency_errors)
        order = []
    else:
        validation, order = validate_workflow_graph(graph, tools, subworkflows)
    return _with_request_id(
        Response({"status": "completed", "validation": validation, "normalized": {"semantic_digest": validation.get("source", {}).get("digest"), "topological_calls": order}}),
        request_id,
    )


@api_view(["POST"])
def compile_graph(request):
    request_id = _request_id(request)
    graph = request.data.get("workflow_graph")
    tools = request.data.get("tool_specs", [])
    if not isinstance(graph, dict) or not isinstance(tools, list):
        return _with_request_id(
            Response(
                {"error": {"code": "REQUEST_INVALID", "message": "workflow_graph and tool_specs are required.", "request_id": request_id}},
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    subworkflows, dependency_errors = _resolve_subworkflow_specs(graph)
    if dependency_errors:
        validation = _dependency_validation(graph, dependency_errors)
        artifacts = []
    else:
        validation, artifacts = compile_workflow(graph, tools, subworkflows)
    succeeded = validation["status"] == "valid"
    workflow = WorkflowDocument.objects.filter(slug=graph.get("id", "")).first()
    workflow_version = None
    if workflow:
        requested_version = request.data.get("workflow_version")
        versions = workflow.versions.all()
        if requested_version is not None:
            try:
                workflow_version = versions.filter(version=int(requested_version)).first()
            except (TypeError, ValueError):
                workflow_version = None
        else:
            workflow_version = versions.filter(
                semantic_digest=validation.get("source", {}).get("digest", "")
            ).first()
    CompilationRecord.objects.create(
        workflow=workflow,
        workflow_version=workflow_version,
        request_id=request_id,
        status="succeeded" if succeeded else "rejected",
        semantic_digest=validation.get("source", {}).get("digest", ""),
        validation=validation,
        artifacts=artifacts,
    )
    wdl_revision = None
    if succeeded and workflow:
        wdl_artifact = next(
            (item for item in artifacts if item.get("name") == "workflow.wdl"),
            None,
        )
        if wdl_artifact:
            wdl_revision = _create_wdl_revision(
                workflow=workflow,
                workflow_version=workflow_version,
                source=WDLRevision.Source.SYSTEM,
                content=wdl_artifact["content"],
                validation={"status": "valid", "diagnostics": []},
            )
    response = Response(
        {
            "status": "succeeded" if succeeded else "rejected",
            "request_id": request_id,
            "validation": validation,
            "artifacts": artifacts,
            "wdl_revision": (
                _wdl_revision_payload(wdl_revision) if wdl_revision else None
            ),
        },
        status=status.HTTP_201_CREATED if succeeded else status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
    return _with_request_id(response, request_id)


def _document_payload(document: WorkflowDocument) -> dict:
    return {
        "slug": document.slug,
        "name": document.name,
        "description": document.description,
        "kind": document.kind,
        "workflow_graph": document.workflow_graph,
        "editor_document": document.editor_document,
        "tool_specs": document.tool_specs,
        "subworkflow_references": document.subworkflow_references,
        "latest_version": document.versions.aggregate(value=Max("version"))["value"],
        "updated_at": document.updated_at.isoformat(),
    }


@api_view(["GET", "PUT"])
def workflow_document(request, slug: str):
    request_id = _request_id(request)
    if request.method == "GET":
        document = WorkflowDocument.objects.filter(slug=slug).first()
        if not document:
            return _with_request_id(
                Response({"error": {"code": "WORKFLOW_NOT_FOUND", "message": "Workflow not found.", "request_id": request_id}}, status=404),
                request_id,
            )
        return _with_request_id(Response(_document_payload(document)), request_id)

    graph = request.data.get("workflow_graph")
    tools = request.data.get("tool_specs")
    editor = request.data.get("editor_document", {})
    if not isinstance(graph, dict) or not isinstance(tools, list) or not isinstance(editor, dict):
        return _with_request_id(
            Response({"error": {"code": "REQUEST_INVALID", "message": "Invalid workflow document.", "request_id": request_id}}, status=400),
            request_id,
        )
    existing = WorkflowDocument.objects.filter(slug=slug).first()
    kind = request.data.get(
        "kind",
        existing.kind if existing else WorkflowDocument.Kind.WORKFLOW,
    )
    if kind not in WorkflowDocument.Kind.values:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WORKFLOW_KIND_INVALID",
                        "message": "kind must be workflow or subworkflow.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    references = request.data.get(
        "subworkflow_references",
        existing.subworkflow_references if existing else [],
    )
    if not isinstance(references, list):
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "SUBWORKFLOW_REFERENCES_INVALID",
                        "message": "subworkflow_references must be a list.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    reference_errors = _validate_subworkflow_references(references)
    if reference_errors:
        return _with_request_id(
            Response(
                {
                    "status": "rejected",
                    "error": {
                        "code": "SUBWORKFLOW_REFERENCE_INVALID",
                        "message": "A subworkflow reference is not an exact published version.",
                        "details": reference_errors,
                        "request_id": request_id,
                    },
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
            request_id,
        )
    document, _ = WorkflowDocument.objects.update_or_create(
        slug=slug,
        defaults={
            "name": request.data.get("name") or graph.get("name") or slug,
            "description": request.data.get(
                "description", existing.description if existing else ""
            ),
            "kind": kind,
            "workflow_graph": graph,
            "editor_document": editor,
            "tool_specs": tools,
            "subworkflow_references": references,
        },
    )
    return _with_request_id(Response(_document_payload(document)), request_id)


@api_view(["GET"])
def compilation_history(request, slug: str):
    request_id = _request_id(request)
    records = CompilationRecord.objects.filter(workflow__slug=slug)[:20]
    body = {
        "results": [
            {
                "id": record.id,
                "request_id": record.request_id,
                "status": record.status,
                "semantic_digest": record.semantic_digest,
                "workflow_version": (
                    record.workflow_version.version
                    if record.workflow_version_id
                    else None
                ),
                "validation": record.validation,
                "artifacts": record.artifacts,
                "created_at": record.created_at.isoformat(),
            }
            for record in records
        ]
    }
    return _with_request_id(Response(body), request_id)


def _tool_version_payload(tool_version: ToolVersion, *, include_spec=False) -> dict:
    payload = {
        "tool_id": tool_version.tool_id,
        "version": tool_version.version,
        "name": tool_version.name,
        "digest": tool_version.digest,
        "created_at": tool_version.created_at.isoformat(),
    }
    if include_spec:
        payload["tool_spec"] = tool_version.tool_spec
    return payload


def _tool_document_payload(document: ToolDocument) -> dict:
    return {
        "tool_id": document.tool_id,
        "draft_spec": document.draft_spec,
        "validation": document.validation,
        "updated_at": document.updated_at.isoformat(),
    }


def _validate_tool_draft(tool_id: str, tool_spec: dict) -> dict:
    validation = validate_tool_spec(tool_spec)
    if tool_spec.get("id") != tool_id:
        validation["diagnostics"].append(
            {
                "code": "TOOL_ID_MISMATCH",
                "stage": "schema",
                "severity": "error",
                "message": "tool_spec.id must match the URL tool_id.",
                "path": "/id",
            }
        )
        validation["status"] = "invalid"
        validation["summary"]["error_count"] += 1
    return validation


@api_view(["GET"])
def tools(request):
    request_id = _request_id(request)
    results = []
    tool_ids = sorted(
        set(ToolVersion.objects.values_list("tool_id", flat=True))
        | set(ToolDocument.objects.values_list("tool_id", flat=True))
    )
    for tool_id in tool_ids:
        latest = ToolVersion.objects.filter(tool_id=tool_id).first()
        draft = ToolDocument.objects.filter(tool_id=tool_id).first()
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
                "version_count": ToolVersion.objects.filter(tool_id=tool_id).count(),
                "draft_status": (
                    draft.validation.get("status", "unknown") if draft else None
                ),
                "draft_updated_at": draft.updated_at.isoformat() if draft else None,
            }
        )
    return _with_request_id(Response({"results": results}), request_id)


@api_view(["GET", "PUT", "POST"])
def tool_document(request, tool_id: str):
    request_id = _request_id(request)
    document = ToolDocument.objects.filter(tool_id=tool_id).first()
    if request.method == "GET":
        if not document:
            return _with_request_id(
                Response(
                    {
                        "error": {
                            "code": "TOOL_DRAFT_NOT_FOUND",
                            "message": "Tool draft not found.",
                            "request_id": request_id,
                        }
                    },
                    status=status.HTTP_404_NOT_FOUND,
                ),
                request_id,
            )
        return _with_request_id(Response(_tool_document_payload(document)), request_id)

    tool_spec = request.data.get("tool_spec")
    if not isinstance(tool_spec, dict):
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "REQUEST_INVALID",
                        "message": "tool_spec is required.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    validation = _validate_tool_draft(tool_id, tool_spec)
    document, _ = ToolDocument.objects.update_or_create(
        tool_id=tool_id,
        defaults={"draft_spec": tool_spec, "validation": validation},
    )
    return _with_request_id(Response(_tool_document_payload(document)), request_id)


def _publish_tool_spec(tool_id: str, tool_spec: dict, request_id: str) -> Response:
    if tool_spec.get("id") != tool_id:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "TOOL_ID_MISMATCH",
                        "message": "tool_spec.id must match the URL tool_id.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    validation = validate_tool_spec(tool_spec)
    if validation["status"] != "valid":
        return _with_request_id(
            Response(
                {"status": "rejected", "validation": validation},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
            request_id,
        )
    version = str(tool_spec.get("tool_version", "")).strip()
    digest = canonical_digest(tool_spec)
    existing = ToolVersion.objects.filter(tool_id=tool_id, version=version).first()
    if existing:
        if existing.digest == digest:
            return _with_request_id(
                Response(_tool_version_payload(existing, include_spec=True)),
                request_id,
            )
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "TOOL_VERSION_IMMUTABLE",
                        "message": "This tool version already exists with different content.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_409_CONFLICT,
            ),
            request_id,
        )
    item = ToolVersion.objects.create(
        tool_id=tool_id,
        version=version,
        name=tool_spec.get("display_name") or tool_spec.get("name") or tool_id,
        digest=digest,
        tool_spec=tool_spec,
    )
    return _with_request_id(
        Response(
            _tool_version_payload(item, include_spec=True),
            status=status.HTTP_201_CREATED,
        ),
        request_id,
    )


@api_view(["POST"])
def publish_tool_document(request, tool_id: str):
    request_id = _request_id(request)
    document = ToolDocument.objects.filter(tool_id=tool_id).first()
    if not document:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "TOOL_DRAFT_NOT_FOUND",
                        "message": "Tool draft not found.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    return _publish_tool_spec(tool_id, document.draft_spec, request_id)


@api_view(["GET", "POST"])
def tool_versions(request, tool_id: str):
    request_id = _request_id(request)
    if request.method == "GET":
        versions = ToolVersion.objects.filter(tool_id=tool_id)
        return _with_request_id(
            Response(
                {
                    "tool_id": tool_id,
                    "results": [
                        _tool_version_payload(item) for item in versions
                    ],
                }
            ),
            request_id,
        )

    tool_spec = request.data.get("tool_spec")
    if not isinstance(tool_spec, dict):
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "REQUEST_INVALID",
                        "message": "tool_spec is required.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    return _publish_tool_spec(tool_id, tool_spec, request_id)


@api_view(["GET"])
def tool_version_detail(request, tool_id: str, version: str):
    request_id = _request_id(request)
    item = ToolVersion.objects.filter(tool_id=tool_id, version=version).first()
    if not item:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "TOOL_VERSION_NOT_FOUND",
                        "message": "Tool version not found.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    return _with_request_id(
        Response(_tool_version_payload(item, include_spec=True)), request_id
    )


@api_view(["GET"])
def workflow_documents(request):
    request_id = _request_id(request)
    documents = WorkflowDocument.objects.all()
    return _with_request_id(
        Response(
            {
                "results": [
                    {
                        "slug": item.slug,
                        "name": item.name,
                        "description": item.description,
                        "kind": item.kind,
                        "latest_version": item.versions.aggregate(
                            value=Max("version")
                        )["value"],
                        "updated_at": item.updated_at.isoformat(),
                    }
                    for item in documents
                ]
            }
        ),
        request_id,
    )


def _workflow_version_payload(
    workflow_version: WorkflowVersion, *, include_snapshot=False
) -> dict:
    payload = {
        "slug": workflow_version.workflow.slug,
        "version": workflow_version.version,
        "name": workflow_version.name,
        "description": workflow_version.description,
        "kind": workflow_version.kind,
        "semantic_digest": workflow_version.semantic_digest,
        "interface_contract": workflow_version.interface_contract,
        "subworkflow_references": workflow_version.subworkflow_references,
        "created_at": workflow_version.created_at.isoformat(),
    }
    if include_snapshot:
        payload.update(
            {
                "workflow_graph": workflow_version.workflow_graph,
                "editor_document": workflow_version.editor_document,
                "tool_specs": workflow_version.tool_specs,
            }
        )
    return payload


@api_view(["GET", "POST"])
def workflow_versions(request, slug: str):
    request_id = _request_id(request)
    document = WorkflowDocument.objects.filter(slug=slug).first()
    if not document:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WORKFLOW_NOT_FOUND",
                        "message": "Workflow not found.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    if request.method == "GET":
        return _with_request_id(
            Response(
                {
                    "slug": slug,
                    "results": [
                        _workflow_version_payload(item)
                        for item in document.versions.all()
                    ],
                }
            ),
            request_id,
        )

    subworkflows, dependency_errors = _resolve_subworkflow_specs(
        document.workflow_graph
    )
    if dependency_errors:
        validation = _dependency_validation(
            document.workflow_graph, dependency_errors
        )
    else:
        validation, artifacts = compile_workflow(
            document.workflow_graph, document.tool_specs, subworkflows
        )
    if validation["status"] != "valid":
        return _with_request_id(
            Response(
                {"status": "rejected", "validation": validation},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
            request_id,
        )
    reference_errors = _validate_subworkflow_references(
        document.subworkflow_references
    )
    if reference_errors:
        return _with_request_id(
            Response(
                {
                    "status": "rejected",
                    "error": {
                        "code": "SUBWORKFLOW_REFERENCE_INVALID",
                        "message": "A subworkflow reference no longer resolves exactly.",
                        "details": reference_errors,
                        "request_id": request_id,
                    },
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
            request_id,
        )
    reuse_unchanged = request.data.get("reuse_unchanged") is True
    files = {
        item["name"]: item["content"]
        for item in artifacts
        if item.get("media_type") == "application/wdl"
    }
    compiled_bundle = {
        "entrypoint": "workflow.wdl",
        "files": files,
        "call_count": sum(
            1
            for node in document.workflow_graph.get("nodes", [])
            if node.get("type") in {"tool", "subworkflow"}
        ),
    }
    compiled_digest = canonical_digest(compiled_bundle)
    reused = False
    with transaction.atomic():
        locked = WorkflowDocument.objects.select_for_update().get(pk=document.pk)
        if (
            locked.updated_at != document.updated_at
            or locked.workflow_graph != document.workflow_graph
            or locked.tool_specs != document.tool_specs
            or locked.subworkflow_references != document.subworkflow_references
        ):
            return _with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WORKFLOW_CHANGED_DURING_PUBLISH",
                            "message": "Workflow 在发布编译期间发生变化，请重新发布。",
                            "request_id": request_id,
                        }
                    },
                    status=status.HTTP_409_CONFLICT,
                ),
                request_id,
            )
        latest = locked.versions.aggregate(value=Max("version"))["value"] or 0
        latest_item = locked.versions.filter(version=latest).first()
        current_digest = validation.get("source", {}).get("digest", "")
        if (
            reuse_unchanged
            and latest_item
            and latest_item.semantic_digest == current_digest
            and latest_item.name == locked.name
            and latest_item.description == locked.description
            and latest_item.kind == locked.kind
            and latest_item.subworkflow_references
            == locked.subworkflow_references
            and latest_item.compiled_digest == compiled_digest
        ):
            item = latest_item
            reused = True
        else:
            item = WorkflowVersion.objects.create(
                workflow=locked,
                version=latest + 1,
                name=locked.name,
                description=locked.description,
                kind=locked.kind,
                semantic_digest=current_digest,
                workflow_graph=locked.workflow_graph,
                editor_document=locked.editor_document,
                tool_specs=locked.tool_specs,
                compiled_bundle=compiled_bundle,
                compiled_digest=compiled_digest,
                compiler_profile="compiler-core-v1",
                interface_contract=_extract_interface_contract(
                    locked.workflow_graph
                ),
                subworkflow_references=locked.subworkflow_references,
            )
    payload = _workflow_version_payload(item, include_snapshot=True)
    payload["reused"] = reused
    return _with_request_id(
        Response(
            payload,
            status=(
                status.HTTP_200_OK
                if reused
                else status.HTTP_201_CREATED
            ),
        ),
        request_id,
    )


@api_view(["GET"])
def workflow_version_detail(request, slug: str, version: int):
    request_id = _request_id(request)
    item = WorkflowVersion.objects.filter(
        workflow__slug=slug, version=version
    ).first()
    if not item:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WORKFLOW_VERSION_NOT_FOUND",
                        "message": "Workflow version not found.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    return _with_request_id(
        Response(_workflow_version_payload(item, include_snapshot=True)),
        request_id,
    )


def _extract_interface_contract(graph: dict) -> dict:
    def port_payload(node: dict) -> dict:
        port = node.get("port") or {}
        return {
            "name": node.get("id"),
            "label": node.get("label") or node.get("id"),
            "wdl_type": port.get("wdl_type"),
            "semantic_type": port.get("semantic_type"),
            "required": bool(port.get("required", False)),
        }

    nodes = graph.get("nodes", [])
    return {
        "contract_version": "1.0.0",
        "inputs": [
            port_payload(node)
            for node in nodes
            if node.get("type") == "workflow_input"
        ],
        "outputs": [
            port_payload(node)
            for node in nodes
            if node.get("type") == "workflow_output"
        ],
    }


def _validate_subworkflow_references(references: list) -> list[dict]:
    errors = []
    for index, reference in enumerate(references):
        if not isinstance(reference, dict):
            errors.append({"index": index, "reason": "reference must be an object"})
            continue
        slug = reference.get("slug")
        version = reference.get("version")
        digest = reference.get("digest")
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = None
        target = WorkflowVersion.objects.filter(
            workflow__slug=slug,
            kind=WorkflowDocument.Kind.SUBWORKFLOW,
            version=version,
        ).first()
        if not target:
            errors.append(
                {"index": index, "slug": slug, "version": version, "reason": "not found"}
            )
        elif digest != target.semantic_digest:
            errors.append(
                {
                    "index": index,
                    "slug": slug,
                    "version": version,
                    "reason": "digest mismatch",
                    "expected_digest": target.semantic_digest,
                }
            )
    return errors


def _wdl_revision_payload(item: WDLRevision, *, include_content=False) -> dict:
    payload = {
        "id": item.id,
        "workflow_slug": item.workflow.slug,
        "version": item.version,
        "source": item.source,
        "digest": item.digest,
        "workflow_version": (
            item.workflow_version.version if item.workflow_version_id else None
        ),
        "validation": item.validation,
        "created_at": item.created_at.isoformat(),
    }
    if include_content:
        payload["content"] = item.content
    return payload


def _create_wdl_revision(
    *,
    workflow: WorkflowDocument,
    workflow_version: WorkflowVersion | None,
    source: str,
    content: str,
    validation: dict,
) -> WDLRevision:
    with transaction.atomic():
        locked = WorkflowDocument.objects.select_for_update().get(pk=workflow.pk)
        latest = locked.wdl_revisions.aggregate(value=Max("version"))["value"] or 0
        return WDLRevision.objects.create(
            workflow=locked,
            workflow_version=workflow_version,
            version=latest + 1,
            source=source,
            content=content,
            digest="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            validation=validation,
        )


@api_view(["GET", "POST"])
def wdl_revisions(request, slug: str):
    request_id = _request_id(request)
    workflow = WorkflowDocument.objects.filter(slug=slug).first()
    if not workflow:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WORKFLOW_NOT_FOUND",
                        "message": "Workflow not found.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    if request.method == "GET":
        return _with_request_id(
            Response(
                {
                    "workflow_slug": slug,
                    "results": [
                        _wdl_revision_payload(item)
                        for item in workflow.wdl_revisions.all()
                    ],
                }
            ),
            request_id,
        )

    content = request.data.get("content")
    if not isinstance(content, str) or not content.strip():
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_CONTENT_REQUIRED",
                        "message": "content must be a non-empty WDL document.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    requested_source = request.data.get("source", WDLRevision.Source.MANUAL)
    if requested_source != WDLRevision.Source.MANUAL:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_SOURCE_INVALID",
                        "message": "Only source=manual can be created through this endpoint.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
            request_id,
        )
    diagnostics = validate_wdl(content)
    validation = {
        "status": "valid" if not diagnostics else "invalid",
        "diagnostics": diagnostics,
    }
    if diagnostics:
        return _with_request_id(
            Response(
                {"status": "rejected", "validation": validation},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
            request_id,
        )
    workflow_version = None
    requested_version = request.data.get("workflow_version")
    if requested_version is not None:
        try:
            workflow_version = workflow.versions.filter(
                version=int(requested_version)
            ).first()
        except (TypeError, ValueError):
            workflow_version = None
        if not workflow_version:
            return _with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WORKFLOW_VERSION_NOT_FOUND",
                            "message": "The requested workflow version does not exist.",
                            "request_id": request_id,
                        }
                    },
                    status=status.HTTP_404_NOT_FOUND,
                ),
                request_id,
            )
    item = _create_wdl_revision(
        workflow=workflow,
        workflow_version=workflow_version,
        source=WDLRevision.Source.MANUAL,
        content=content,
        validation=validation,
    )
    return _with_request_id(
        Response(
            _wdl_revision_payload(item, include_content=True),
            status=status.HTTP_201_CREATED,
        ),
        request_id,
    )


@api_view(["GET"])
def wdl_revision_detail(request, slug: str, version: int):
    request_id = _request_id(request)
    item = WDLRevision.objects.filter(
        workflow__slug=slug, version=version
    ).first()
    if not item:
        return _with_request_id(
            Response(
                {
                    "error": {
                        "code": "WDL_REVISION_NOT_FOUND",
                        "message": "WDL revision not found.",
                        "request_id": request_id,
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            ),
            request_id,
        )
    return _with_request_id(
        Response(_wdl_revision_payload(item, include_content=True)), request_id
    )

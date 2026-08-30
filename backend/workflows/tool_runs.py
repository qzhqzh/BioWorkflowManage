from __future__ import annotations

import mimetypes
import os
import re
import stat as stat_module
from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from compiler_core import canonical_digest, compile_workflow, validate_tool_spec

from .analysis_runs import (
    _LegacyOutputLimit,
    _accessible_run_path,
    _flatten_outputs,
    _format_size,
    _run_timing_payload,
)
from .models import AnalysisRun, AnalysisRunEvent, ToolVersion
from .integration_outputs import (
    ResourceSnapshotBudget,
    ResourceSnapshotBudgetError,
    ResourceSnapshotChangedError,
    open_verified_output,
    output_manifest_file_item_is_verified,
    output_manifest_has_integrity_v2,
    output_value_limit_reason,
)


MANAGED_ROOTS = {
    "rawdata": ("ANALYSIS_RAWDATA_ROOT", "ANALYSIS_RAWDATA_EXECUTION_ROOT"),
    "database": ("ANALYSIS_DATABASE_ROOT", "ANALYSIS_DATABASE_EXECUTION_ROOT"),
}


class ToolRunInputError(ValueError):
    def __init__(self, code: str, message: str, *, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_username()
    return "local-user"


def _actor_user(request):
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    return None


def _error(error: ToolRunInputError, http_status=status.HTTP_400_BAD_REQUEST):
    payload: dict[str, Any] = {"code": error.code, "message": str(error)}
    if error.details is not None:
        payload["details"] = error.details
    return Response({"error": payload}, status=http_status)


def _visible_tool_runs(request):
    queryset = AnalysisRun.objects.select_related("tool_version").filter(
        run_kind=AnalysisRun.Kind.TOOL_TEST
    )
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
            return queryset
        return queryset.filter(submitted_by=user)
    return queryset.filter(actor="local-user")


def _safe_identifier(prefix: str, index: int, name: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return f"{prefix}{index}_{suffix}"[:128]


def _tool_test_bundle(
    item: ToolVersion,
) -> tuple[dict[str, Any], str, dict[str, str], dict[str, str]]:
    tool = item.tool_spec
    if canonical_digest(tool) != item.digest:
        raise ToolRunInputError(
            "TOOL_VERSION_DIGEST_MISMATCH",
            "工具版本内容摘要不匹配，不能用于测试。",
        )
    validation = validate_tool_spec(tool)
    if validation["status"] != "valid":
        raise ToolRunInputError(
            "TOOL_VERSION_INVALID",
            "工具版本未通过 ToolSpec 校验。",
            details={"diagnostics": validation["diagnostics"]},
        )
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    input_nodes: dict[str, str] = {}
    output_nodes: dict[str, str] = {}
    call_id = "task_under_test"
    for index, port in enumerate(tool.get("inputs", []), 1):
        node_id = _safe_identifier("input_", index, str(port["name"]))
        input_nodes[str(port["name"])] = node_id
        nodes.append(
            {
                "id": node_id,
                "type": "workflow_input",
                "label": str(port.get("label") or port["name"]),
                "port": {
                    "name": "value",
                    "wdl_type": port["wdl_type"],
                    "semantic_type": port["semantic_type"],
                    "required": bool(port.get("required", True)),
                    **(
                        {"description": str(port["description"])}
                        if port.get("description")
                        else {}
                    ),
                },
            }
        )
        edges.append(
            {
                "id": _safe_identifier("edge_input_", index, str(port["name"])),
                "source": {"node_id": node_id, "port": "value"},
                "target": {"node_id": call_id, "port": port["name"]},
            }
        )
    nodes.append(
        {
            "id": call_id,
            "type": "tool",
            "label": item.name,
            "tool_ref": {
                "id": item.tool_id,
                "tool_version": item.version,
                "spec_version": tool["schema_version"],
                "digest": item.digest,
            },
            "parameter_values": {},
        }
    )
    for index, port in enumerate(tool.get("outputs", []), 1):
        node_id = _safe_identifier("output_", index, str(port["name"]))
        output_nodes[str(port["name"])] = node_id
        nodes.append(
            {
                "id": node_id,
                "type": "workflow_output",
                "label": str(port.get("label") or port["name"]),
                "port": {
                    "name": "value",
                    "wdl_type": port["wdl_type"],
                    "semantic_type": port["semantic_type"],
                    "required": not bool(port.get("optional", False)),
                    **(
                        {"description": str(port["description"])}
                        if port.get("description")
                        else {}
                    ),
                },
            }
        )
        edges.append(
            {
                "id": _safe_identifier("edge_output_", index, str(port["name"])),
                "source": {"node_id": call_id, "port": port["name"]},
                "target": {"node_id": node_id, "port": "value"},
            }
        )
    target_version = "development" if any(
        port.get("wdl_type") == "Directory"
        for port in [*tool.get("inputs", []), *tool.get("outputs", [])]
    ) else "1.0"
    graph = {
        "schema_version": "1.0.0",
        "id": "tool_test",
        "name": f"{item.name} 独立测试",
        "description": f"系统生成的 {item.tool_id}@{item.version} 独立测试运行。",
        "target": {
            "language": "wdl",
            "version": target_version,
            "profile": "miniwdl-compatible",
        },
        "nodes": nodes,
        "edges": edges,
        "layout": {"nodes": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}},
        "metadata": {"tags": ["tool-test"], "notes": "internal execution artifact"},
    }
    compiled, artifacts = compile_workflow(graph, [tool])
    if compiled["status"] != "valid":
        raise ToolRunInputError(
            "TOOL_TEST_WDL_INVALID",
            "工具无法生成可运行的单 Task WDL。",
            details={"diagnostics": compiled["diagnostics"]},
        )
    artifact_map = {artifact["name"]: artifact["content"] for artifact in artifacts}
    bundle = {
        "schema_version": 1,
        "entrypoint": "tool-test.wdl",
        "files": {"tool-test.wdl": artifact_map["workflow.wdl"]},
        "call_count": 1,
        "tool": {
            "id": item.tool_id,
            "version": item.version,
            "digest": item.digest,
        },
    }
    output_labels = {
        f"tool_test.{output_nodes[str(port['name'])]}": str(
            port.get("label") or port["name"]
        )
        for port in tool.get("outputs", [])
    }
    return bundle, canonical_digest(bundle), input_nodes, output_labels


def _managed_resource(
    value: Any,
    *,
    kind: str,
    input_name: str,
    manifests: dict[str, list[dict[str, Any]]],
    snapshot_budget: ResourceSnapshotBudget,
) -> str:
    if not isinstance(value, dict):
        raise ToolRunInputError(
            "TOOL_TEST_INPUT_INVALID",
            f"输入 {input_name} 必须从受管原始数据或数据库目录选择。",
        )
    source = str(value.get("source") or "")
    relative_value = str(value.get("path") or "").strip()
    if source not in MANAGED_ROOTS or not relative_value or "\x00" in relative_value:
        raise ToolRunInputError(
            "TOOL_TEST_INPUT_INVALID",
            f"输入 {input_name} 的资源来源或路径无效。",
        )
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ToolRunInputError(
            "TOOL_TEST_INPUT_INVALID",
            f"输入 {input_name} 必须使用受管目录内的相对路径。",
        )
    try:
        snapshot_budget.claim_item()
    except ResourceSnapshotBudgetError as error:
        raise ToolRunInputError("TOOL_TEST_RESOURCE_LIMIT_EXCEEDED", str(error)) from error
    local_setting, execution_setting = MANAGED_ROOTS[source]
    local_root = Path(getattr(settings, local_setting)).resolve()
    execution_root = Path(getattr(settings, execution_setting)).resolve()
    local_path = (local_root / relative).resolve()
    try:
        normalized = local_path.relative_to(local_root)
    except ValueError as error:
        raise ToolRunInputError(
            "TOOL_TEST_INPUT_INVALID",
            f"输入 {input_name} 越过受管目录。",
        ) from error
    try:
        stat = os.stat(local_path, follow_symlinks=False)
    except OSError:
        stat = None
    expected_mode = stat_module.S_ISDIR if kind == "directory" else stat_module.S_ISREG
    if stat is None or not expected_mode(stat.st_mode):
        raise ToolRunInputError(
            "TOOL_TEST_RESOURCE_MISSING",
            f"输入 {input_name} 的资源不存在：{normalized.as_posix()}",
        )
    directory_manifest = None
    if kind == "directory":
        try:
            directory_manifest = snapshot_budget.directory_manifest(
                local_path,
                containment_root=local_root,
            )
        except ResourceSnapshotBudgetError as error:
            raise ToolRunInputError(
                "TOOL_TEST_RESOURCE_LIMIT_EXCEEDED",
                str(error),
            ) from error
        except ResourceSnapshotChangedError as error:
            raise ToolRunInputError(
                "TOOL_TEST_RESOURCE_CHANGED",
                str(error),
            ) from error
        except (OSError, ValueError) as error:
            raise ToolRunInputError(
                "TOOL_TEST_RESOURCE_UNSUPPORTED",
                f"输入 {input_name} 的目录包含不受支持的节点。",
            ) from error
        identity = directory_manifest["identity"]
    else:
        try:
            identity = snapshot_budget.file_identity(
                local_path,
                containment_root=local_root,
            )
        except (OSError, ValueError) as error:
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID",
                f"输入 {input_name} 越过受管目录或包含符号链接。",
            ) from error
    manifest_item: dict[str, Any] = {
        "relative_path": normalized.as_posix(),
        "kind": kind,
        "input": input_name,
        "verification": (
            "directory_identity_sha256" if kind == "directory" else "identity_v2"
        ),
        "identity": {
            "mtime_ns": identity["mtime_ns"],
            "ctime_ns": identity["ctime_ns"],
            "device": identity["device"],
            "inode": identity["inode"],
        },
    }
    if kind == "directory":
        manifest_item.update(
            {
                "digest": directory_manifest["digest"],
                "entry_count": directory_manifest["entry_count"],
            }
        )
    else:
        manifest_item.update(
            {
                "size": identity["size"],
                "mtime_ns": identity["mtime_ns"],
                "ctime_ns": identity["ctime_ns"],
                "device": identity["device"],
                "inode": identity["inode"],
            }
        )
    manifests[source].append(manifest_item)
    return str(execution_root / normalized)


def _validate_constraints(port: dict[str, Any], value: Any) -> None:
    constraints = port.get("constraints") or {}
    name = str(port["name"])
    if "enum" in constraints and value not in constraints["enum"]:
        raise ToolRunInputError(
            "TOOL_TEST_INPUT_INVALID", f"输入 {name} 不在允许值范围内。"
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in constraints and value < constraints["minimum"]:
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 小于最小值。"
            )
        if "maximum" in constraints and value > constraints["maximum"]:
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 大于最大值。"
            )
    if isinstance(value, (str, list)):
        if "min_length" in constraints and len(value) < constraints["min_length"]:
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 长度不足。"
            )
        if "max_length" in constraints and len(value) > constraints["max_length"]:
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 长度超出限制。"
            )
    if isinstance(value, str) and constraints.get("pattern"):
        try:
            matched = re.fullmatch(str(constraints["pattern"]), value)
        except re.error as error:
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 的约束表达式无效。"
            ) from error
        if matched is None:
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 格式不符合要求。"
            )


def _coerce_input(
    port: dict[str, Any],
    value: Any,
    manifests: dict[str, list[dict[str, Any]]],
    snapshot_budget: ResourceSnapshotBudget,
) -> Any:
    name = str(port["name"])
    wdl_type = str(port["wdl_type"])
    if wdl_type == "File":
        result = _managed_resource(
            value,
            kind="file",
            input_name=name,
            manifests=manifests,
            snapshot_budget=snapshot_budget,
        )
    elif wdl_type == "Directory":
        result = _managed_resource(
            value,
            kind="directory",
            input_name=name,
            manifests=manifests,
            snapshot_budget=snapshot_budget,
        )
    elif wdl_type == "Pair[File,File]":
        if not isinstance(value, list) or len(value) != 2:
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 需要两个文件。"
            )
        pair = [
            _managed_resource(
                item,
                kind="file",
                input_name=name,
                manifests=manifests,
                snapshot_budget=snapshot_budget,
            )
            for item in value
        ]
        result = {"left": pair[0], "right": pair[1]}
    elif wdl_type == "Array[File]":
        if not isinstance(value, list):
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 必须是文件数组。"
            )
        result = [
            _managed_resource(
                item,
                kind="file",
                input_name=name,
                manifests=manifests,
                snapshot_budget=snapshot_budget,
            )
            for item in value
        ]
    elif wdl_type == "String":
        if not isinstance(value, str):
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 必须是字符串。"
            )
        result = value
    elif wdl_type == "Int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 必须是整数。"
            )
        result = value
    elif wdl_type == "Float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 必须是数字。"
            )
        result = float(value)
    elif wdl_type == "Boolean":
        if not isinstance(value, bool):
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 必须是布尔值。"
            )
        result = value
    elif wdl_type.startswith("Array["):
        if not isinstance(value, list):
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 必须是数组。"
            )
        expected = wdl_type[6:-1]
        checks = {
            "String": lambda item: isinstance(item, str),
            "Int": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "Float": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "Boolean": lambda item: isinstance(item, bool),
        }
        if expected not in checks or not all(checks[expected](item) for item in value):
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_INVALID", f"输入 {name} 的数组元素类型不正确。"
            )
        result = [float(item) for item in value] if expected == "Float" else value
    else:
        raise ToolRunInputError(
            "TOOL_TEST_INPUT_UNSUPPORTED", f"暂不支持输入类型 {wdl_type}。"
        )
    _validate_constraints(port, result)
    return result


def _prepare_inputs(
    item: ToolVersion,
    raw_inputs: Any,
    input_nodes: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw_inputs, dict):
        raise ToolRunInputError("TOOL_TEST_INPUT_INVALID", "inputs 必须是 JSON object。")
    ports = {str(port["name"]): port for port in item.tool_spec.get("inputs", [])}
    unknown = sorted(set(raw_inputs) - set(ports))
    if unknown:
        raise ToolRunInputError(
            "TOOL_TEST_INPUT_INVALID",
            f"包含未知输入：{', '.join(unknown)}。",
        )
    manifests: dict[str, list[dict[str, Any]]] = {"rawdata": [], "database": []}
    snapshot_budget = ResourceSnapshotBudget()
    values: dict[str, Any] = {}
    for name, port in ports.items():
        has_value = name in raw_inputs and raw_inputs[name] is not None
        if has_value:
            raw_value = raw_inputs[name]
        elif "default" in port:
            raw_value = port["default"]
        elif port.get("required", True):
            raise ToolRunInputError(
                "TOOL_TEST_INPUT_REQUIRED", f"请填写必填输入 {name}。"
            )
        else:
            continue
        values[f"tool_test.{input_nodes[name]}"] = _coerce_input(
            port,
            raw_value,
            manifests,
            snapshot_budget,
        )
    resource_manifests = {
        "input_resource_manifest": (
            {"files": manifests["rawdata"]} if manifests["rawdata"] else None
        ),
        "database_resource_manifest": (
            {"resources": manifests["database"]} if manifests["database"] else None
        ),
    }
    return values, resource_manifests


def _tool_output_payload(run: AnalysisRun) -> list[dict[str, Any]]:
    output_labels = dict(run.request_payload.get("output_labels") or {})
    for index, port in enumerate(run.tool_version.tool_spec.get("outputs", []), 1):
        name = str(port["name"])
        key = f"tool_test.{_safe_identifier('output_', index, name)}"
        output_labels.setdefault(key, str(port.get("label") or name))

    manifest = run.output_manifest if isinstance(run.output_manifest, dict) else {}
    if output_manifest_has_integrity_v2(manifest):
        payload = []
        items = manifest.get("items")
        if not isinstance(items, list):
            return payload
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            kind = str(item.get("kind") or "value")
            output = {
                "key": key,
                "label": str(output_labels.get(key) or item.get("label") or key),
                "kind": kind,
            }
            if kind == "file":
                output["name"] = str(
                    item.get("filename") or item.get("name") or key
                )
                size = item.get("size")
                if isinstance(size, int):
                    output["size"] = size
                    output["size_label"] = _format_size(size)
                if output_manifest_file_item_is_verified(item):
                    output["download_url"] = (
                        f"/api/v1/tool-test-runs/{run.id}/outputs"
                        f"?key={quote(key, safe='')}"
                    )
            elif kind == "value":
                reason = output_value_limit_reason(item.get("value"))
                if reason is None:
                    output["value"] = item.get("value")
                else:
                    output["kind"] = "unverifiable"
                    output["reason"] = reason
            elif "entry_count" in item:
                output["entry_count"] = item["entry_count"]
            if item.get("reason"):
                output["reason"] = str(item["reason"])
            payload.append(output)
        return payload

    if not run.work_directory or not run.outputs:
        return []
    try:
        root = _accessible_run_path(Path(run.work_directory))
    except (OSError, ValueError):
        return []
    outputs = run.outputs.get("outputs", run.outputs)
    payload = []
    for key, value in _flatten_outputs(outputs):
        label = str(output_labels.get(key) or key)
        if isinstance(value, _LegacyOutputLimit):
            payload.append(
                {
                    "key": key,
                    "label": label,
                    "kind": "unverifiable",
                    "reason": value.reason,
                }
            )
            continue
        if isinstance(value, str):
            try:
                resolved = _accessible_run_path(Path(value))
                resolved.relative_to(root)
            except (OSError, ValueError):
                resolved = None
            if resolved is not None and resolved.is_file():
                payload.append(
                    {
                        "key": key,
                        "label": label,
                        "kind": "file",
                        "name": resolved.name,
                        "size": resolved.stat().st_size,
                        "size_label": _format_size(resolved.stat().st_size),
                    }
                )
                continue
        payload.append({"key": key, "label": label, "kind": "value", "value": value})
    return payload


def tool_run_payload(
    run: AnalysisRun,
    *,
    include_events: bool = False,
    include_outputs: bool = True,
    include_task_timing: bool = True,
    include_request: bool = True,
    include_error_details: bool = True,
) -> dict[str, Any]:
    payload = {
        "id": str(run.id),
        "tool": {
            "id": run.tool_version.tool_id,
            "version": run.tool_version.version,
            "name": run.tool_version.name,
            "digest": run.tool_version.digest,
        },
        "label": run.sample_name,
        "actor": run.actor,
        "status": run.status,
        "output_status": run.output_status,
        "progress": run.progress,
        "current_step": run.current_step,
        "request": run.request_payload if include_request else {},
        "error": run.error if include_error_details else "",
        "error_code": run.error_code,
        "error_category": run.error_category,
        "error_retryable": run.error_retryable,
        "error_details": run.error_details if include_error_details else {},
        "outputs": _tool_output_payload(run) if include_outputs else [],
        "timing": _run_timing_payload(
            run,
            include_task_timing=include_task_timing,
        ),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.updated_at,
    }
    if include_events:
        payload["events"] = [
            {
                "id": event.id,
                "kind": event.kind,
                "level": event.level,
                "message": event.message,
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in run.events.all()[:500]
        ]
    return payload


@api_view(["GET"])
def tool_test_resources(request):
    source = str(request.query_params.get("source") or "rawdata")
    kind = str(request.query_params.get("kind") or "file")
    query = str(request.query_params.get("q") or "").strip().casefold()
    if source not in MANAGED_ROOTS or kind not in {"file", "directory"}:
        return _error(
            ToolRunInputError("TOOL_TEST_RESOURCE_QUERY_INVALID", "资源查询参数无效。")
        )
    root = Path(getattr(settings, MANAGED_ROOTS[source][0])).resolve()
    if not root.is_dir():
        return Response({"source": source, "kind": kind, "results": []})
    results: list[dict[str, Any]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        candidates = directories if kind == "directory" else files
        for name in sorted(candidates):
            path = Path(current) / name
            try:
                relative = path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if query and query not in relative.casefold():
                continue
            item: dict[str, Any] = {"path": relative, "name": name, "kind": kind}
            if kind == "file":
                try:
                    item["size"] = path.stat().st_size
                    item["size_label"] = _format_size(item["size"])
                except OSError:
                    continue
            results.append(item)
            if len(results) >= 500:
                return Response(
                    {"source": source, "kind": kind, "results": results, "truncated": True}
                )
    return Response({"source": source, "kind": kind, "results": results})


@api_view(["GET", "POST"])
def tool_test_runs(request):
    if request.method == "GET":
        queryset = _visible_tool_runs(request).defer(
            "outputs",
            "output_manifest",
            "request_payload",
            "input_values",
            "source_bundle",
            "error",
            "error_details",
            "work_directory",
            "tool_version__tool_spec",
        )
        tool_id = str(request.query_params.get("tool_id") or "").strip()
        version = str(request.query_params.get("version") or "").strip()
        if tool_id:
            queryset = queryset.filter(tool_version__tool_id=tool_id)
        if version:
            queryset = queryset.filter(tool_version__version=version)
        return Response(
            {
                "view": "summary",
                "results": [
                    tool_run_payload(
                        run,
                        include_outputs=False,
                        include_task_timing=False,
                        include_request=False,
                        include_error_details=False,
                    )
                    for run in queryset[:50]
                ]
            }
        )

    tool_id = str(request.data.get("tool_id") or "").strip()
    version = str(request.data.get("tool_version") or "").strip()
    item = ToolVersion.objects.filter(tool_id=tool_id, version=version).first()
    if item is None:
        return _error(
            ToolRunInputError("TOOL_VERSION_NOT_FOUND", "所选工具版本不存在。"),
            status.HTTP_404_NOT_FOUND,
        )
    try:
        bundle, source_digest, input_nodes, output_labels = _tool_test_bundle(item)
        input_values, resource_manifests = _prepare_inputs(
            item, request.data.get("inputs") or {}, input_nodes
        )
    except ToolRunInputError as error:
        return _error(error)
    label = str(request.data.get("label") or f"{item.name} 测试").strip()[:256]
    run = AnalysisRun.objects.create(
        run_kind=AnalysisRun.Kind.TOOL_TEST,
        tool_version=item,
        workflow_name="tool_test",
        sample_id=f"tool-test-{item.tool_id}"[:256],
        sample_name=label,
        actor=_actor(request),
        submitted_by=_actor_user(request),
        source_bundle=bundle,
        source_digest=source_digest,
        request_payload={
            "kind": "tool_test",
            "tool_id": item.tool_id,
            "tool_version": item.version,
            "tool_digest": item.digest,
            "managed_inputs": request.data.get("inputs") or {},
            "compiled_source_digest": source_digest,
            "output_labels": output_labels,
            **resource_manifests,
        },
        input_values=input_values,
    )
    AnalysisRunEvent.objects.create(run=run, message="工具测试已进入队列。")
    return Response(
        tool_run_payload(run, include_events=True),
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
def tool_test_run_detail(request, run_id):
    run = get_object_or_404(
        _visible_tool_runs(request).prefetch_related("events"), pk=run_id
    )
    return Response(tool_run_payload(run, include_events=True))


@api_view(["GET"])
def tool_test_run_output(request, run_id):
    run = get_object_or_404(_visible_tool_runs(request), pk=run_id)
    key = str(request.query_params.get("key") or "")
    manifest = run.output_manifest if isinstance(run.output_manifest, dict) else {}
    if output_manifest_has_integrity_v2(manifest):
        items = manifest.get("items")
        if not isinstance(items, list):
            return _error(
                ToolRunInputError(
                    "ANALYSIS_OUTPUT_UNVERIFIED",
                    "输出完整性清单无效，无法安全下载。",
                ),
                status.HTTP_409_CONFLICT,
            )
        item = next(
            (
                value
                for value in items
                if isinstance(value, dict) and value.get("key") == key
            ),
            None,
        )
        if item is None:
            return _error(
                ToolRunInputError("TOOL_TEST_OUTPUT_NOT_FOUND", "输出文件不存在。"),
                status.HTTP_404_NOT_FOUND,
            )
        if item.get("kind") == "unverifiable":
            return _error(
                ToolRunInputError(
                    "ANALYSIS_OUTPUT_INCOMPLETE",
                    "输出项未完成或完整性清单无效，无法安全下载。",
                ),
                status.HTTP_409_CONFLICT,
            )
        if item.get("kind") != "file":
            return _error(
                ToolRunInputError("TOOL_TEST_OUTPUT_NOT_FOUND", "输出文件不存在。"),
                status.HTTP_404_NOT_FOUND,
            )
        if not output_manifest_file_item_is_verified(item):
            return _error(
                ToolRunInputError(
                    "ANALYSIS_OUTPUT_UNVERIFIED",
                    "输出项完整性清单无效，无法安全下载。",
                ),
                status.HTTP_409_CONFLICT,
            )
        try:
            path, handle = open_verified_output(
                item,
                run_root=run.work_directory,
            )
        except (KeyError, OSError, TypeError, ValueError):
            return _error(
                ToolRunInputError(
                    "ANALYSIS_OUTPUT_CHANGED",
                    "输出文件与成功时的不可变清单不一致。",
                ),
                status.HTTP_409_CONFLICT,
            )
    else:
        legacy_output = next(
            (
                value
                for value in _tool_output_payload(run)
                if value.get("kind") == "file" and value.get("key") == key
            ),
            None,
        )
        if legacy_output is None:
            return _error(
                ToolRunInputError("TOOL_TEST_OUTPUT_NOT_FOUND", "输出文件不存在。"),
                status.HTTP_404_NOT_FOUND,
            )
        return _error(
            ToolRunInputError(
                "ANALYSIS_OUTPUT_UNVERIFIED",
                "历史输出缺少完整性清单，无法安全下载。",
            ),
            status.HTTP_409_CONFLICT,
        )
    content_type = str(
        item.get("content_type")
        or mimetypes.guess_type(str(item.get("filename") or path.name))[0]
        or "application/octet-stream"
    )
    response = FileResponse(
        handle,
        as_attachment=True,
        filename=str(item.get("filename") or path.name),
        content_type=content_type,
    )
    response["ETag"] = f'"{str(item["sha256"]).removeprefix("sha256:")}"'
    return response

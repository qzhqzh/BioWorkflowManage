from __future__ import annotations

import re
import textwrap
from copy import deepcopy
from typing import Any

import WDL
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from compiler_core import canonical_digest, validate_tool_spec, validate_workflow_graph

from .models import (
    ToolDocument,
    ToolVersion,
    WDLGraphProposal,
    WDLRevision,
    WorkflowDocument,
)
from .request_ids import request_id, with_request_id


SUPPORTED_WDL_TYPES = {
    "File",
    "Directory",
    "String",
    "Int",
    "Float",
    "Boolean",
    "Array[File]",
    "Array[String]",
    "Array[Int]",
    "Array[Float]",
    "Array[Boolean]",
}
REFERENCE_PATTERN = re.compile(
    r"^(?P<node>[A-Za-z_][A-Za-z0-9_]*)(?:\.(?P<port>[A-Za-z_][A-Za-z0-9_]*))?$"
)


class ProposalBlocked(ValueError):
    pass


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_username()
    return "local-user"


def _document_digest(document: WorkflowDocument) -> str:
    return canonical_digest(
        {
            "name": document.name,
            "description": document.description,
            "kind": document.kind,
            "workflow_graph": document.workflow_graph,
            "editor_document": document.editor_document,
            "tool_specs": document.tool_specs,
            "subworkflow_references": document.subworkflow_references,
        }
    )


def _semantic_type(wdl_type: str) -> str:
    return {
        "File": "core.file.any",
        "Directory": "core.directory",
        "String": "core.string",
        "Int": "core.integer",
        "Float": "core.float",
        "Boolean": "core.boolean",
        "Array[File]": "core.array.file",
        "Array[String]": "core.array.string",
        "Array[Int]": "core.array.integer",
        "Array[Float]": "core.array.float",
        "Array[Boolean]": "core.array.boolean",
    }.get(wdl_type, "core.string")


def _normalized_type(raw_type: str) -> tuple[str, bool]:
    optional = raw_type.endswith("?")
    wdl_type = raw_type[:-1] if optional else raw_type
    if wdl_type not in SUPPORTED_WDL_TYPES:
        raise ProposalBlocked(f"画布暂不支持 WDL 类型 {raw_type}。")
    return wdl_type, optional


def _literal_value(expression) -> Any:
    if expression is None:
        raise ProposalBlocked("表达式为空，无法映射为画布参数。")
    if hasattr(expression, "value"):
        return expression.value
    parts = getattr(expression, "parts", None)
    if parts is not None and all(isinstance(item, str) for item in parts):
        if len(parts) >= 2 and parts[0] in {'"', "'"} and parts[-1] == parts[0]:
            return "".join(parts[1:-1])
        return "".join(parts)
    items = getattr(expression, "items", None)
    if items is not None:
        return [_literal_value(item) for item in items]
    raise ProposalBlocked(f"表达式 {expression} 不是可安全映射的字面量。")


def _simple_reference(expression) -> tuple[str, str | None] | None:
    match = REFERENCE_PATTERN.fullmatch(str(expression).strip())
    if not match:
        return None
    return match.group("node"), match.group("port")


def _port_from_declaration(declaration, existing: dict | None, *, output=False) -> dict:
    wdl_type, optional = _normalized_type(str(declaration.type))
    port = {
        "name": declaration.name,
        "wdl_type": wdl_type,
        "semantic_type": (
            (existing or {}).get("semantic_type")
            if (existing or {}).get("wdl_type") == wdl_type
            else _semantic_type(wdl_type)
        ),
    }
    if existing is None or "label" in existing:
        port["label"] = (existing or {}).get("label", declaration.name)
    for key in ("description", "constraints"):
        if key in (existing or {}):
            port[key] = deepcopy(existing[key])
    if output:
        try:
            literal = _literal_value(declaration.expr)
        except ProposalBlocked:
            port["capture"] = {"mode": "expression", "value": str(declaration.expr)}
        else:
            port["capture"] = {"mode": "path", "value": literal}
        if optional:
            port["optional"] = True
        return port
    port["required"] = not optional and declaration.expr is None
    if declaration.expr is not None:
        port["default"] = _literal_value(declaration.expr)
    return port


def _command_template(task) -> tuple[str, bool]:
    chunks: list[str] = []
    for part in task.command.parts:
        if isinstance(part, str):
            chunks.append(part)
            continue
        if getattr(part, "options", None):
            raise ProposalBlocked(
                f"task {task.name} 的 command 占位符 {part} 含格式选项，无法无损映射。"
            )
        reference = _simple_reference(getattr(part, "expr", None))
        if reference is None or reference[1] is not None:
            raise ProposalBlocked(
                f"task {task.name} 的 command 含复杂占位表达式 {part}，需要人工维护 ToolSpec。"
            )
        chunks.append(f"{{{{ inputs.{reference[0]} }}}}")
    command = textwrap.dedent("".join(chunks)).strip("\n")
    lines = command.splitlines()
    strict_mode = bool(lines and lines[0].strip() == "set -euo pipefail")
    if strict_mode:
        lines = lines[1:]
    return textwrap.dedent("\n".join(lines)).strip("\n") + "\n", strict_mode


def _runtime(task) -> tuple[dict, str]:
    values = getattr(task, "runtime", None) or getattr(task, "requirements", None) or {}
    supported_keys = {"docker", "container", "cpu", "memory", "disks", "disk"}
    unsupported_keys = sorted(set(values) - supported_keys)
    if unsupported_keys:
        raise ProposalBlocked(
            f"task {task.name} 含暂不支持的 runtime 字段：{', '.join(unsupported_keys)}。"
        )
    docker = values.get("docker") or values.get("container")
    if docker is None:
        raise ProposalBlocked(f"task {task.name} 没有固定 docker/container runtime。")
    image = str(_literal_value(docker)).strip()
    if not image:
        raise ProposalBlocked(f"task {task.name} 的容器镜像为空。")
    runtime: dict[str, Any] = {}
    if values.get("cpu") is not None:
        runtime["cpu"] = max(1, int(_literal_value(values["cpu"])))
    for source_name, target_name in (
        ("memory", "memory_gb"),
        ("disks", "disk_gb"),
        ("disk", "disk_gb"),
    ):
        if values.get(source_name) is None or target_name in runtime:
            continue
        rendered = str(_literal_value(values[source_name]))
        match = re.search(
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:G|GB|GiB)", rendered, re.IGNORECASE
        )
        if not match:
            raise ProposalBlocked(
                f"task {task.name} 的 runtime {source_name}={rendered} 无法无损换算为 GB。"
            )
        value = float(match.group(1))
        runtime[target_name] = int(value) if target_name == "disk_gb" else value
    return runtime, image


def _version_bound_fields(spec: dict) -> dict:
    fields = {
        key: deepcopy(spec.get(key))
        for key in (
            "container",
            "inputs",
            "outputs",
            "command",
            "runtime",
            "task_kind",
            "annotation",
        )
        if key in spec
    }
    for key in ("inputs", "outputs"):
        if key in fields:
            fields[key] = sorted(fields[key], key=lambda item: item["name"])
    return fields


VERSION_BOUND_FIELD_LABELS = {
    "container": "容器",
    "inputs": "输入接口",
    "outputs": "输出接口",
    "command": "命令模板",
    "runtime": "运行资源",
    "task_kind": "任务类型",
    "annotation": "注释配置",
}


def _version_bound_field_diffs(current: dict, candidate: dict) -> list[dict]:
    before = _version_bound_fields(current)
    after = _version_bound_fields(candidate)
    return [
        {
            "field": key,
            "label": label,
            "before": deepcopy(before.get(key)),
            "after": deepcopy(after.get(key)),
        }
        for key, label in VERSION_BOUND_FIELD_LABELS.items()
        if before.get(key) != after.get(key)
    ]


def _draft_version(tool_id: str, current_version: str, revision_version: int) -> str:
    stem = re.sub(r"-wdl\.\d+$", "", current_version or "0.1.0")
    candidate = f"{stem}-wdl.{revision_version}"
    suffix = 2
    while ToolVersion.objects.filter(tool_id=tool_id, version=candidate).exists():
        candidate = f"{stem}-wdl.{revision_version}.{suffix}"
        suffix += 1
    return candidate[:128]


def _tool_spec_from_task(
    task, current: dict | None, revision: WDLRevision
) -> tuple[dict, bool, list[str], list[dict]]:
    current = deepcopy(current or {})
    current_inputs = {item["name"]: item for item in current.get("inputs", [])}
    current_outputs = {item["name"]: item for item in current.get("outputs", [])}
    runtime, image = _runtime(task)
    runtime = {**deepcopy(current.get("runtime", {})), **runtime}
    command, strict_mode = _command_template(task)
    tool_id = current.get("id") or task.name
    candidate = {
        **current,
        "schema_version": current.get("schema_version", "1.0.0"),
        "id": tool_id,
        "name": current.get("name", task.name),
        "display_name": current.get("display_name", task.name),
        "tool_version": current.get("tool_version", "0.1.0"),
        "description": current.get(
            "description", f"从 WDL task {task.name} 映射的工具草稿。"
        ),
        "category": current.get("category", "wdl_mapped"),
        "container": {"engine": "docker", "image": image},
        "inputs": [
            _port_from_declaration(item, current_inputs.get(item.name))
            for item in (task.inputs or [])
        ],
        "outputs": [
            _port_from_declaration(item, current_outputs.get(item.name), output=True)
            for item in (task.outputs or [])
        ],
        "command": {
            "shell": current.get("command", {}).get("shell", "bash"),
            "strict_mode": strict_mode,
            "template": command,
        },
        "runtime": runtime,
    }
    field_diffs = _version_bound_field_diffs(current, candidate)
    changed_fields = [item["label"] for item in field_diffs]
    changed = not current or bool(changed_fields)
    if not changed:
        return current, False, [], []
    if changed:
        candidate["tool_version"] = _draft_version(
            tool_id, current.get("tool_version", "0.1.0"), revision.version
        )
        metadata = deepcopy(current.get("metadata", {}))
        metadata["source_wdl"] = {
            "workflow_slug": revision.workflow.slug,
            "revision": revision.version,
            "file_path": f"{revision.workflow.slug}.wdl",
            "task_name": task.name,
            "source_digest": revision.digest,
        }
        candidate["metadata"] = metadata
    validation = validate_tool_spec(candidate)
    if validation["status"] != "valid":
        messages = "; ".join(item["message"] for item in validation["diagnostics"][:3])
        raise ProposalBlocked(f"task {task.name} 无法映射为有效 ToolSpec：{messages}")
    return candidate, changed, changed_fields, field_diffs


def _preserved_port(existing_node: dict | None, declaration, *, required: bool) -> dict:
    wdl_type, optional = _normalized_type(str(declaration.type))
    existing_port = (existing_node or {}).get("port", {})
    return {
        "name": "value",
        "wdl_type": wdl_type,
        "semantic_type": (
            existing_port.get("semantic_type")
            if existing_port.get("wdl_type") == wdl_type
            else _semantic_type(wdl_type)
        ),
        **({"required": not optional and declaration.expr is None} if required else {}),
        **(
            {"description": existing_port["description"]}
            if existing_port.get("description")
            else {}
        ),
    }


def _edge_id(
    source_node: str,
    source_port: str,
    target_node: str,
    target_port: str,
    existing: dict[tuple[str, str, str, str], str],
) -> str:
    key = (source_node, source_port, target_node, target_port)
    return existing.get(
        key,
        f"edge_{source_node}_{source_port}_{target_node}_{target_port}"[:128],
    )


def _layout_for_nodes(
    document: WorkflowDocument, nodes: list[dict]
) -> tuple[dict, dict]:
    existing_layout = document.workflow_graph.get("layout", {})
    positions = deepcopy(existing_layout.get("nodes", {}))
    counters = {"workflow_input": 0, "tool": 0, "workflow_output": 0}
    columns = {"workflow_input": 80, "tool": 360, "workflow_output": 720}
    for node in nodes:
        if node["id"] in positions:
            continue
        kind = node["type"]
        positions[node["id"]] = {"x": columns[kind], "y": 80 + counters[kind] * 160}
        counters[kind] += 1
    positions = {node["id"]: positions[node["id"]] for node in nodes}
    viewport = deepcopy(
        existing_layout.get("viewport")
        or document.editor_document.get("viewport")
        or {"x": 0, "y": 0, "zoom": 1}
    )
    return (
        {"nodes": positions, "viewport": viewport},
        {
            "nodes": [
                {"id": node["id"], "position": positions[node["id"]]} for node in nodes
            ],
            "viewport": viewport,
        },
    )


def _change_rows(
    current: dict, proposed: dict, tool_changes: list[dict]
) -> dict[str, list[dict]]:
    current_nodes = {item["id"]: item for item in current.get("nodes", [])}
    proposed_nodes = {item["id"]: item for item in proposed.get("nodes", [])}
    current_edges = {
        (
            item["source"]["node_id"],
            item["source"]["port"],
            item["target"]["node_id"],
            item["target"]["port"],
        )
        for item in current.get("edges", [])
    }
    proposed_edges = {
        (
            item["source"]["node_id"],
            item["source"]["port"],
            item["target"]["node_id"],
            item["target"]["port"],
        )
        for item in proposed.get("edges", [])
    }
    structure: list[dict] = []
    instance: list[dict] = []
    for node_id in sorted(set(proposed_nodes) - set(current_nodes)):
        structure.append(
            {
                "kind": "node_added",
                "subject": node_id,
                "detail": proposed_nodes[node_id]["type"],
            }
        )
    for node_id in sorted(set(current_nodes) - set(proposed_nodes)):
        structure.append(
            {
                "kind": "node_removed",
                "subject": node_id,
                "detail": current_nodes[node_id]["type"],
            }
        )
    for node_id in sorted(set(current_nodes) & set(proposed_nodes)):
        before = current_nodes[node_id]
        after = proposed_nodes[node_id]
        before_parameters = before.get("parameter_values", {})
        after_parameters = after.get("parameter_values", {})
        for parameter in sorted(set(before_parameters) | set(after_parameters)):
            before_value = before_parameters.get(parameter, "使用工具默认值")
            after_value = after_parameters.get(parameter, "使用工具默认值")
            if before_value == after_value:
                continue
            instance.append(
                {
                    "kind": "parameter_changed",
                    "subject": f"{node_id}.{parameter}",
                    "detail": f"{before_value} → {after_value}",
                }
            )
        ignored = {"parameter_values"}
        if (
            before.get("type") == "tool"
            and after.get("type") == "tool"
            and before.get("tool_ref", {}).get("id")
            == after.get("tool_ref", {}).get("id")
        ):
            ignored.add("tool_ref")
        structural_before = {
            key: value for key, value in before.items() if key not in ignored
        }
        structural_after = {
            key: value for key, value in after.items() if key not in ignored
        }
        if structural_before != structural_after:
            structure.append(
                {"kind": "node_changed", "subject": node_id, "detail": after["type"]}
            )
    for edge in sorted(proposed_edges - current_edges):
        structure.append(
            {
                "kind": "edge_added",
                "subject": f"{edge[0]}.{edge[1]} → {edge[2]}.{edge[3]}",
                "detail": "",
            }
        )
    for edge in sorted(current_edges - proposed_edges):
        structure.append(
            {
                "kind": "edge_removed",
                "subject": f"{edge[0]}.{edge[1]} → {edge[2]}.{edge[3]}",
                "detail": "",
            }
        )
    return {
        "workflow_structure": structure,
        "tool_versions": tool_changes,
        "instance_parameters": instance,
    }


def build_proposal(document: WorkflowDocument, revision: WDLRevision) -> dict:
    try:
        parsed = WDL.parse_document(revision.content, uri=f"{document.slug}.wdl")
        parsed.typecheck()
    except Exception as error:
        raise ProposalBlocked(f"WDL 无法解析或通过类型检查：{error}") from error
    if parsed.imports:
        raise ProposalBlocked(
            "第一版仅映射单文件直接 call；含 import 的 WDL 请先在历史工作台维护。"
        )
    workflow = parsed.workflow
    if workflow is None:
        raise ProposalBlocked("WDL 中没有 workflow。")
    if str(parsed.wdl_version) != "1.0":
        raise ProposalBlocked(f"画布当前只支持 WDL 1.0，检测到 {parsed.wdl_version}。")
    unsupported = [item for item in workflow.body if type(item).__name__ != "Call"]
    if unsupported:
        kinds = ", ".join(sorted({type(item).__name__ for item in unsupported}))
        raise ProposalBlocked(f"workflow 含暂不支持的动态结构：{kinds}。")

    current_nodes = {
        item["id"]: item for item in document.workflow_graph.get("nodes", [])
    }
    current_edge_ids = {
        (
            item["source"]["node_id"],
            item["source"]["port"],
            item["target"]["node_id"],
            item["target"]["port"],
        ): item["id"]
        for item in document.workflow_graph.get("edges", [])
    }
    current_specs_by_digest = {
        canonical_digest(item): item for item in document.tool_specs
    }
    task_specs: dict[str, dict] = {}
    tool_changes: list[dict] = []
    tool_drafts: list[dict] = []
    for task in parsed.tasks:
        current_spec = None
        for call in workflow.body:
            if call.callee_id[-1] != task.name:
                continue
            node = current_nodes.get(call.name, {})
            current_spec = current_specs_by_digest.get(
                node.get("tool_ref", {}).get("digest")
            )
            if current_spec:
                break
        if current_spec is None:
            current_spec = next(
                (item for item in document.tool_specs if item.get("id") == task.name),
                None,
            )
        proposed_spec, changed, changed_fields, field_diffs = _tool_spec_from_task(
            task, current_spec, revision
        )
        task_specs[task.name] = proposed_spec
        if changed:
            existing_draft = ToolDocument.objects.filter(
                tool_id=proposed_spec["id"]
            ).first()
            if existing_draft and existing_draft.draft_spec != proposed_spec:
                raise ProposalBlocked(
                    f"工具 {proposed_spec['id']} 已有未发布草稿；请先发布或处理该草稿，再应用 WDL 提案。"
                )
            tool_changes.append(
                {
                    "kind": "tool_draft_created",
                    "subject": proposed_spec["id"],
                    "detail": (
                        f"{(current_spec or {}).get('tool_version', '新工具')} → "
                        f"{proposed_spec['tool_version']}；固定内容："
                        f"{'、'.join(changed_fields) or '新工具定义'}"
                    ),
                }
            )
            tool_drafts.append(
                {
                    "tool_id": proposed_spec["id"],
                    "base_version": (current_spec or {}).get("tool_version"),
                    "proposed_version": proposed_spec["tool_version"],
                    "changed_fields": changed_fields or ["新工具定义"],
                    "field_diffs": field_diffs,
                    "tool_spec": proposed_spec,
                }
            )

    nodes: list[dict] = []
    edges: list[dict] = []
    for declaration in workflow.inputs or []:
        if declaration.expr is not None:
            raise ProposalBlocked(
                f"workflow input {declaration.name} 含默认值，当前画布契约无法无损表达。"
            )
        existing = current_nodes.get(declaration.name)
        nodes.append(
            {
                "id": declaration.name,
                "type": "workflow_input",
                "label": (existing or {}).get("label", declaration.name),
                "port": _preserved_port(existing, declaration, required=True),
            }
        )

    call_names = {call.name for call in workflow.body}
    workflow_input_names = {item.name for item in workflow.inputs or []}
    for call in workflow.body:
        task_name = call.callee_id[-1]
        spec = task_specs.get(task_name)
        if spec is None:
            raise ProposalBlocked(
                f"call {call.name} 引用的 {task_name} 不是当前文件中的 task。"
            )
        existing = current_nodes.get(call.name)
        parameters: dict[str, Any] = deepcopy(
            (existing or {}).get("parameter_values", {})
        )
        for port_name, expression in call.inputs.items():
            reference = _simple_reference(expression)
            if reference:
                parameters.pop(port_name, None)
                source_node, source_port = reference
                if source_node in workflow_input_names and source_port is None:
                    source_port = "value"
                elif source_node in call_names and source_port:
                    pass
                else:
                    raise ProposalBlocked(
                        f"call {call.name}.{port_name} 引用了无法映射的表达式 {expression}。"
                    )
                edges.append(
                    {
                        "id": _edge_id(
                            source_node,
                            source_port,
                            call.name,
                            port_name,
                            current_edge_ids,
                        ),
                        "source": {"node_id": source_node, "port": source_port},
                        "target": {"node_id": call.name, "port": port_name},
                    }
                )
            else:
                parameters[port_name] = _literal_value(expression)
        nodes.append(
            {
                "id": call.name,
                "type": "tool",
                "label": (existing or {}).get("label", call.name),
                "tool_ref": {
                    "id": spec["id"],
                    "tool_version": spec["tool_version"],
                    "spec_version": spec["schema_version"],
                    "digest": canonical_digest(spec),
                },
                **({"parameter_values": parameters} if parameters else {}),
            }
        )

    for declaration in workflow.outputs or []:
        if str(declaration.type).endswith("?"):
            raise ProposalBlocked(
                f"workflow output {declaration.name} 是可选类型，当前画布契约无法无损表达。"
            )
        reference = _simple_reference(declaration.expr)
        if reference is None:
            raise ProposalBlocked(
                f"workflow output {declaration.name} 使用复杂表达式 {declaration.expr}。"
            )
        source_node, source_port = reference
        if source_node in workflow_input_names and source_port is None:
            source_port = "value"
        elif source_node in call_names and source_port:
            pass
        else:
            raise ProposalBlocked(
                f"workflow output {declaration.name} 引用了无法映射的表达式 {declaration.expr}。"
            )
        existing = current_nodes.get(declaration.name)
        nodes.append(
            {
                "id": declaration.name,
                "type": "workflow_output",
                "label": (existing or {}).get("label", declaration.name),
                "port": _preserved_port(existing, declaration, required=False),
            }
        )
        edges.append(
            {
                "id": _edge_id(
                    source_node,
                    source_port,
                    declaration.name,
                    "value",
                    current_edge_ids,
                ),
                "source": {"node_id": source_node, "port": source_port},
                "target": {"node_id": declaration.name, "port": "value"},
            }
        )

    layout, editor_document = _layout_for_nodes(document, nodes)
    proposed_graph = {
        **deepcopy(document.workflow_graph),
        "schema_version": "1.0.0",
        "id": document.slug,
        "name": document.name,
        "description": document.description,
        "target": {
            "language": "wdl",
            "version": "1.0",
            "profile": document.workflow_graph.get("target", {}).get(
                "profile", "miniwdl-compatible"
            ),
        },
        "nodes": nodes,
        "edges": edges,
        "layout": layout,
    }
    mapped_tool_ids = {spec["id"] for spec in task_specs.values()}
    proposed_specs = [task_specs[name] for name in sorted(task_specs)] + [
        deepcopy(spec)
        for spec in document.tool_specs
        if spec.get("id") not in mapped_tool_ids
    ]
    validation, _ = validate_workflow_graph(proposed_graph, proposed_specs)
    if validation["status"] != "valid":
        messages = "; ".join(item["message"] for item in validation["diagnostics"][:5])
        raise ProposalBlocked(f"映射后的画布未通过验证：{messages}")

    changes = _change_rows(document.workflow_graph, proposed_graph, tool_changes)
    required_confirmations = [key for key, rows in changes.items() if rows]
    return {
        "schema_version": "1.0.0",
        "source": {
            "workflow_slug": document.slug,
            "wdl_revision": revision.version,
            "wdl_digest": revision.digest,
        },
        "base": {
            "document_version": document.document_version,
            "document_digest": _document_digest(document),
        },
        "summary": {
            "workflow_change_count": len(changes["workflow_structure"]),
            "tool_draft_count": len(tool_drafts),
            "instance_change_count": len(changes["instance_parameters"]),
        },
        "changes": changes,
        "required_confirmations": required_confirmations,
        "warnings": [
            "应用只更新可编辑画布草稿；已发布 WorkflowVersion 和 ToolVersion 保持不变。",
            "新工具内容进入未发布草稿，需单独审查并发布。",
        ],
        "proposed_graph": proposed_graph,
        "proposed_editor_document": editor_document,
        "proposed_tool_specs": proposed_specs,
        "tool_drafts": tool_drafts,
    }


def _proposal_payload(item: WDLGraphProposal) -> dict:
    proposal = item.proposal
    return {
        "id": item.id,
        "status": item.status,
        "proposal_digest": item.proposal_digest,
        "workflow_slug": item.workflow.slug,
        "source_revision": item.source_revision.version,
        "source_digest": item.source_revision.digest,
        "base_document_version": item.base_document_version,
        "base_document_digest": item.base_document_digest,
        "summary": proposal.get("summary", {}),
        "changes": proposal.get("changes", {}),
        "required_confirmations": proposal.get("required_confirmations", []),
        "warnings": proposal.get("warnings", []),
        "blocking_issues": proposal.get("blocking_issues", []),
        "tool_drafts": [
            {
                "tool_id": draft["tool_id"],
                "base_version": draft.get("base_version"),
                "proposed_version": draft["proposed_version"],
                "changed_fields": draft.get("changed_fields", []),
                "field_diffs": draft.get("field_diffs", []),
            }
            for draft in proposal.get("tool_drafts", [])
        ],
        "created_by": item.created_by,
        "created_at": item.created_at.isoformat(),
        "applied_by": item.applied_by,
        "applied_document_version": item.applied_document_version,
        "applied_at": item.applied_at.isoformat() if item.applied_at else None,
    }


def _error(code: str, message: str, request_value: str, http_status: int, **details):
    payload = {"code": code, "message": message, "request_id": request_value}
    if details:
        payload["details"] = details
    return with_request_id(
        Response({"error": payload}, status=http_status), request_value
    )


@api_view(["GET", "POST"])
def wdl_graph_proposals(request, slug: str, version: int):
    request_value = request_id(request)
    document = WorkflowDocument.objects.filter(slug=slug).first()
    revision = (
        WDLRevision.objects.filter(workflow=document, version=version).first()
        if document
        else None
    )
    if not document or not revision:
        return _error(
            "WDL_REVISION_NOT_FOUND",
            "找不到用于映射的 WDL 修订。",
            request_value,
            status.HTTP_404_NOT_FOUND,
        )
    if request.method == "GET":
        return with_request_id(
            Response(
                {
                    "results": [
                        _proposal_payload(item)
                        for item in revision.graph_proposals.all()
                    ]
                }
            ),
            request_value,
        )
    base_version = request.data.get("base_document_version")
    base_digest = request.data.get("base_document_digest")
    if base_version is None or not base_digest:
        return _error(
            "WORKFLOW_PRECONDITION_REQUIRED",
            "生成提案前必须携带当前画布版本和摘要。",
            request_value,
            428,
        )
    if base_version != document.document_version or base_digest != _document_digest(
        document
    ):
        return _error(
            "WORKFLOW_DOCUMENT_CONFLICT",
            "画布已经变化，请刷新后重新生成 WDL 变更提案。",
            request_value,
            status.HTTP_409_CONFLICT,
            current_document_version=document.document_version,
            current_document_digest=_document_digest(document),
        )
    try:
        proposal = build_proposal(document, revision)
        proposal_status = WDLGraphProposal.Status.READY
    except ProposalBlocked as error:
        proposal = {
            "schema_version": "1.0.0",
            "source": {
                "workflow_slug": slug,
                "wdl_revision": version,
                "wdl_digest": revision.digest,
            },
            "base": {
                "document_version": document.document_version,
                "document_digest": base_digest,
            },
            "summary": {
                "workflow_change_count": 0,
                "tool_draft_count": 0,
                "instance_change_count": 0,
            },
            "changes": {},
            "required_confirmations": [],
            "warnings": [],
            "blocking_issues": [str(error)],
        }
        proposal_status = WDLGraphProposal.Status.BLOCKED
    proposal_digest = canonical_digest(proposal)
    lookup = {
        "workflow": document,
        "source_revision": revision,
        "base_document_version": document.document_version,
        "proposal_digest": proposal_digest,
    }
    try:
        with transaction.atomic():
            item, _ = WDLGraphProposal.objects.get_or_create(
                **lookup,
                defaults={
                    "base_document_digest": base_digest,
                    "status": proposal_status,
                    "proposal": proposal,
                    "created_by": _actor(request),
                },
            )
    except IntegrityError:
        item = WDLGraphProposal.objects.get(**lookup)
    return with_request_id(
        Response(_proposal_payload(item), status=status.HTTP_201_CREATED),
        request_value,
    )


@api_view(["POST"])
def apply_wdl_graph_proposal(request, slug: str, proposal_id: int):
    request_value = request_id(request)
    proposal_digest = request.data.get("proposal_digest")
    base_version = request.data.get("base_document_version")
    base_digest = request.data.get("base_document_digest")
    if not proposal_digest or base_version is None or not base_digest:
        return _error(
            "WORKFLOW_PRECONDITION_REQUIRED",
            "应用提案必须携带提案摘要和当前画布版本。",
            request_value,
            428,
        )
    confirmations = request.data.get("confirm_sections")
    if not isinstance(confirmations, list) or not all(
        isinstance(section, str) for section in confirmations
    ):
        return _error(
            "PROPOSAL_CONFIRMATIONS_REQUIRED",
            "请逐类确认提案中的变更。",
            request_value,
            status.HTTP_400_BAD_REQUEST,
        )
    actor = _actor(request)
    with transaction.atomic():
        item = (
            WDLGraphProposal.objects.select_for_update()
            .filter(pk=proposal_id, workflow__slug=slug)
            .first()
        )
        if not item:
            return _error(
                "WDL_GRAPH_PROPOSAL_NOT_FOUND", "找不到该变更提案。", request_value, 404
            )
        document = WorkflowDocument.objects.select_for_update().get(pk=item.workflow_id)
        if (
            proposal_digest != item.proposal_digest
            or base_version != item.base_document_version
            or base_digest != item.base_document_digest
        ):
            return _error(
                "WDL_GRAPH_PROPOSAL_CONFLICT",
                "请求中的提案摘要或基线与已保存提案不一致。",
                request_value,
                status.HTTP_409_CONFLICT,
            )
        if item.status == WDLGraphProposal.Status.BLOCKED:
            return _error(
                "WDL_GRAPH_PROPOSAL_BLOCKED",
                "该提案存在无法安全映射的内容，未修改画布。",
                request_value,
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                blocking_issues=item.proposal.get("blocking_issues", []),
            )
        if item.status == WDLGraphProposal.Status.APPLIED:
            return with_request_id(Response(_proposal_payload(item)), request_value)
        current_digest = _document_digest(document)
        if (
            document.document_version != item.base_document_version
            or current_digest != item.base_document_digest
        ):
            return _error(
                "WORKFLOW_DOCUMENT_CONFLICT",
                "画布或提案基线已经变化，当前内容未被覆盖。",
                request_value,
                status.HTTP_409_CONFLICT,
                current_document_version=document.document_version,
                current_document_digest=current_digest,
            )
        required = set(item.proposal.get("required_confirmations", []))
        if set(confirmations) != required:
            return _error(
                "PROPOSAL_CONFIRMATIONS_INCOMPLETE",
                "必须确认所有受影响类别后才能原子应用。",
                request_value,
                status.HTTP_400_BAD_REQUEST,
                required_confirmations=sorted(required),
            )
        validation, _ = validate_workflow_graph(
            item.proposal["proposed_graph"],
            item.proposal["proposed_tool_specs"],
        )
        if validation["status"] != "valid":
            return _error(
                "WDL_GRAPH_PROPOSAL_INVALID",
                "提案已无法通过当前校验规则，未修改画布；请重新生成提案。",
                request_value,
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                diagnostics=validation["diagnostics"],
            )
        drafts = item.proposal.get("tool_drafts", [])
        existing_drafts = {
            existing.tool_id: existing
            for existing in ToolDocument.objects.select_for_update().filter(
                tool_id__in=[draft["tool_id"] for draft in drafts]
            )
        }
        for draft in drafts:
            existing = existing_drafts.get(draft["tool_id"])
            if existing and existing.draft_spec != draft["tool_spec"]:
                return _error(
                    "TOOL_DRAFT_CONFLICT",
                    f"工具 {draft['tool_id']} 已有其他未发布草稿，提案未应用。",
                    request_value,
                    status.HTTP_409_CONFLICT,
                    tool_id=draft["tool_id"],
                )
        for draft in drafts:
            existing = existing_drafts.get(draft["tool_id"])
            if existing:
                continue
            validation = validate_tool_spec(draft["tool_spec"])
            try:
                with transaction.atomic():
                    ToolDocument.objects.create(
                        tool_id=draft["tool_id"],
                        draft_spec=draft["tool_spec"],
                        validation=validation,
                    )
            except IntegrityError:
                concurrent = ToolDocument.objects.select_for_update().get(
                    tool_id=draft["tool_id"]
                )
                if concurrent.draft_spec != draft["tool_spec"]:
                    transaction.set_rollback(True)
                    return _error(
                        "TOOL_DRAFT_CONFLICT",
                        f"工具 {draft['tool_id']} 已有其他未发布草稿，提案未应用。",
                        request_value,
                        status.HTTP_409_CONFLICT,
                        tool_id=draft["tool_id"],
                    )
        document.workflow_graph = item.proposal["proposed_graph"]
        document.editor_document = item.proposal["proposed_editor_document"]
        document.tool_specs = item.proposal["proposed_tool_specs"]
        document.subworkflow_references = []
        document.updated_by = actor
        if any(item.proposal.get("changes", {}).values()):
            document.document_version += 1
            document.save()
        item.status = WDLGraphProposal.Status.APPLIED
        item.applied_by = actor
        item.applied_document_version = document.document_version
        item.applied_at = timezone.now()
        item.save(
            update_fields=[
                "status",
                "applied_by",
                "applied_document_version",
                "applied_at",
            ]
        )
    return with_request_id(
        Response(
            {
                "proposal": _proposal_payload(item),
                "workflow": {
                    "slug": document.slug,
                    "document_version": document.document_version,
                    "document_digest": _document_digest(document),
                    "updated_by": document.updated_by,
                },
            }
        ),
        request_value,
    )

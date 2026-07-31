from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
STAGE_ORDER = {
    stage: index
    for index, stage in enumerate(
        (
            "parse",
            "schema",
            "tool_spec",
            "resolution",
            "graph",
            "type",
            "lowering",
            "render",
            "wdl_validation",
            "system",
        )
    )
}


def canonical_json(document: Any) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_digest(document: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(document).encode()).hexdigest()


def semantic_document(graph: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in graph.items() if key not in {"layout", "metadata"}}
    result["nodes"] = sorted(result.get("nodes", []), key=lambda item: item["id"])
    result["edges"] = sorted(result.get("edges", []), key=lambda item: item["id"])
    return result


def semantic_digest(graph: dict[str, Any]) -> str:
    return canonical_digest(semantic_document(graph))


def diagnostic(
    code: str,
    stage: str,
    message: str,
    *,
    path: str = "",
    location: dict[str, str] | None = None,
    severity: str = "error",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "stage": stage,
        "severity": severity,
        "message": message,
    }
    if path:
        result["path"] = path
    if location:
        result["location"] = location
    return result


def report(
    diagnostics: list[dict[str, Any]],
    *,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics.sort(
        key=lambda item: (
            STAGE_ORDER[item["stage"]],
            0 if item["severity"] == "error" else 1,
            item.get("path", ""),
            item.get("location", {}).get("node_id", ""),
            item.get("location", {}).get("edge_id", ""),
            item["code"],
        )
    )
    errors = sum(item["severity"] == "error" for item in diagnostics)
    warnings = sum(item["severity"] == "warning" for item in diagnostics)
    result: dict[str, Any] = {
        "report_version": "1.0.0",
        "status": "invalid" if errors else "valid",
        "validation_id": f"val_{uuid.uuid4().hex}",
        "summary": {"error_count": errors, "warning_count": warnings},
        "diagnostics": diagnostics,
    }
    if source:
        result["source"] = source
    return result


def schema_diagnostics(document: Any, schema_name: str) -> list[dict[str, Any]]:
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    return [
        diagnostic(
            "SCHEMA001",
            "schema",
            error.message,
            path="/" + "/".join(str(part) for part in error.absolute_path),
        )
        for error in errors
    ]


def validate_tool_spec(tool: dict[str, Any]) -> dict[str, Any]:
    diagnostics = schema_diagnostics(tool, "tool-spec.schema.json")
    image = tool.get("container", {}).get("image", "")
    if image and "@sha256:" not in image:
        diagnostics.append(
            diagnostic(
                "TW001",
                "tool_spec",
                "Container image is tagged but not pinned by digest.",
                path="/container/image",
                severity="warning",
            )
        )
    return report(
        diagnostics,
        source={"kind": "tool_spec", "id": str(tool.get("id", "unknown")), "digest": canonical_digest(tool)},
    )


def _port(node: dict[str, Any], port_name: str, direction: str, callable_spec: dict[str, Any] | None) -> dict[str, Any] | None:
    if node["type"] in {"workflow_input", "workflow_output"}:
        return node.get("port") if port_name == "value" else None
    ports = callable_spec.get("inputs" if direction == "in" else "outputs", []) if callable_spec else []
    return next((port for port in ports if port["name"] == port_name), None)


def _topological_calls(graph: dict[str, Any]) -> tuple[list[str], bool]:
    call_ids = {
        node["id"]
        for node in graph.get("nodes", [])
        if node["type"] in {"tool", "subworkflow"}
    }
    dependencies = {node_id: set() for node_id in call_ids}
    for edge in graph.get("edges", []):
        source = edge["source"]["node_id"]
        target = edge["target"]["node_id"]
        if source in call_ids and target in call_ids:
            dependencies[target].add(source)
    ordered: list[str] = []
    while dependencies:
        ready = sorted(node_id for node_id, deps in dependencies.items() if not deps)
        if not ready:
            return ordered, True
        for node_id in ready:
            ordered.append(node_id)
            dependencies.pop(node_id)
        for deps in dependencies.values():
            deps.difference_update(ready)
    return ordered, False


def validate_workflow_graph(
    graph: dict[str, Any],
    tool_specs: list[dict[str, Any]],
    subworkflow_specs: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    diagnostics = schema_diagnostics(graph, "workflow-graph.schema.json")
    if diagnostics:
        return report(diagnostics), []

    nodes = {node["id"]: node for node in graph["nodes"]}
    tools_by_digest = {canonical_digest(tool): tool for tool in tool_specs}
    subworkflows = {
        (item["slug"], item["version"], item["semantic_digest"]): item
        for item in (subworkflow_specs or [])
    }
    resolved: dict[str, dict[str, Any]] = {}
    inbound: set[tuple[str, str]] = set()

    for node in nodes.values():
        if node["type"] == "subworkflow":
            ref = node["subworkflow_ref"]
            item = subworkflows.get(
                (ref["slug"], ref["version"], ref["digest"])
            )
            if item is None:
                diagnostics.append(
                    diagnostic(
                        "WG021",
                        "resolution",
                        f"SubworkflowRef for {node['id']} does not exactly match a published snapshot.",
                        location={"node_id": node["id"]},
                    )
                )
            else:
                contract = item["interface_contract"]
                if node.get("interface_contract") != contract:
                    diagnostics.append(
                        diagnostic(
                            "WG022",
                            "resolution",
                            f"Interface snapshot for {node['id']} differs from the published version.",
                            location={"node_id": node["id"]},
                        )
                    )
                resolved[node["id"]] = {
                    "inputs": contract.get("inputs", []),
                    "outputs": contract.get("outputs", []),
                }
            continue
        if node["type"] != "tool":
            continue
        ref = node["tool_ref"]
        tool = tools_by_digest.get(ref["digest"])
        if (
            tool is None
            or tool.get("id") != ref["id"]
            or tool.get("tool_version") != ref["tool_version"]
            or tool.get("schema_version") != ref["spec_version"]
        ):
            diagnostics.append(
                diagnostic(
                    "WG008",
                    "resolution",
                    f"ToolRef for {node['id']} does not exactly match the supplied ToolSpec bundle.",
                    location={"node_id": node["id"], "tool_id": ref["id"]},
                )
            )
        else:
            resolved[node["id"]] = tool

    for edge in graph["edges"]:
        source_node = nodes.get(edge["source"]["node_id"])
        target_node = nodes.get(edge["target"]["node_id"])
        location = {"edge_id": edge["id"]}
        if source_node is None or target_node is None:
            diagnostics.append(diagnostic("WG004", "graph", "Edge references an unknown node.", location=location))
            continue
        if source_node["id"] == target_node["id"]:
            diagnostics.append(diagnostic("WG020", "graph", "Self edges are not allowed.", location=location))
        source_port = _port(source_node, edge["source"]["port"], "out", resolved.get(source_node["id"]))
        target_port = _port(target_node, edge["target"]["port"], "in", resolved.get(target_node["id"]))
        if source_port is None or target_port is None:
            diagnostics.append(diagnostic("WG005", "graph", "Edge references an unknown port.", location=location))
            continue
        inbound_key = (target_node["id"], edge["target"]["port"])
        if inbound_key in inbound:
            diagnostics.append(diagnostic("WG007", "graph", "Input port has multiple inbound edges.", location=location))
        inbound.add(inbound_key)
        if source_port["wdl_type"] != target_port["wdl_type"]:
            diagnostics.append(diagnostic("WG012", "type", "WDL type mismatch.", location=location))
        source_semantic = source_port.get("semantic_type")
        target_semantic = target_port.get("semantic_type")
        if source_semantic != target_semantic and "core.file.any" not in {source_semantic, target_semantic}:
            diagnostics.append(diagnostic("WG013", "type", "Semantic type mismatch.", location=location))

    for node_id, tool in resolved.items():
        node = nodes[node_id]
        if node["type"] != "tool":
            for port in tool["inputs"]:
                if port.get("required", False) and (node_id, port["name"]) not in inbound:
                    diagnostics.append(
                        diagnostic(
                            "WG011",
                            "graph",
                            f"Required input {port['name']} is not bound.",
                            location={"node_id": node_id, "port": port["name"]},
                        )
                    )
            continue
        parameters = node.get("parameter_values", {})
        for port in tool["inputs"]:
            if port.get("required", False) and "default" not in port:
                bound = (node_id, port["name"]) in inbound or port["name"] in parameters
                if not bound:
                    diagnostics.append(
                        diagnostic(
                            "WG011",
                            "graph",
                            f"Required input {port['name']} is not bound.",
                            location={"node_id": node_id, "port": port["name"]},
                        )
                    )
            if (node_id, port["name"]) in inbound and port["name"] in parameters:
                diagnostics.append(
                    diagnostic(
                        "WG018",
                        "graph",
                        f"Input {port['name']} is bound by both an edge and a parameter.",
                        location={"node_id": node_id, "port": port["name"]},
                    )
                )

    order, has_cycle = _topological_calls(graph)
    if has_cycle:
        diagnostics.append(diagnostic("WG014", "graph", "Workflow graph contains a cycle."))
    source = {
        "kind": "workflow_graph",
        "id": graph["id"],
        "digest": semantic_digest(graph),
    }
    return report(diagnostics, source=source), order

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .validation import (
    canonical_digest,
    diagnostic,
    report,
    semantic_digest,
    validate_workflow_graph,
)


INPUT_PATTERN = re.compile(r"\{\{\s*inputs\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _type(port: dict[str, Any]) -> dict[str, Any]:
    return {
        "wdl_type": port["wdl_type"],
        "optional": not port.get("required", True),
        "semantic_type": port["semantic_type"],
    }


def _literal(value: Any, wdl_type: str) -> dict[str, Any]:
    return {"kind": "literal", "wdl_type": wdl_type, "value": value}


def _command_segments(template: str) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    cursor = 0
    for match in INPUT_PATTERN.finditer(template):
        if match.start() > cursor:
            segments.append({"kind": "literal", "value": template[cursor : match.start()]})
        segments.append({"kind": "input_ref", "name": match.group(1)})
        cursor = match.end()
    if cursor < len(template):
        segments.append({"kind": "literal", "value": template[cursor:]})
    return segments


def lower_to_ir(
    graph: dict[str, Any],
    tool_specs: list[dict[str, Any]],
    call_order: list[str],
    subworkflow_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = graph["edges"]
    tools = {canonical_digest(tool): tool for tool in tool_specs}
    used_tools = {
        nodes[node_id]["tool_ref"]["digest"]: tools[nodes[node_id]["tool_ref"]["digest"]]
        for node_id in call_order
        if nodes[node_id]["type"] == "tool"
    }
    subworkflows = {
        (item["slug"], item["version"], item["semantic_digest"]): item
        for item in (subworkflow_specs or [])
    }

    tasks = []
    for digest, tool in sorted(used_tools.items(), key=lambda item: item[1]["id"]):
        task_inputs = []
        for port in sorted(tool["inputs"], key=lambda item: item["name"]):
            item: dict[str, Any] = {"name": port["name"], "type": _type(port)}
            if "default" in port:
                item["default"] = _literal(port["default"], port["wdl_type"])
            task_inputs.append(item)
        tasks.append(
            {
                "name": tool["id"],
                "source_tool": {
                    "id": tool["id"],
                    "tool_version": tool["tool_version"],
                    "spec_version": tool["schema_version"],
                    "digest": digest,
                },
                "inputs": task_inputs,
                "command": {
                    "shell": tool["command"]["shell"],
                    "strict_mode": tool["command"].get("strict_mode", True),
                    "segments": _command_segments(tool["command"]["template"]),
                },
                "outputs": [
                    {
                        "name": port["name"],
                        "type": _type(port),
                        "expression": {
                            "kind": port["capture"]["mode"],
                            "value": port["capture"]["value"],
                        },
                    }
                    for port in sorted(tool["outputs"], key=lambda item: item["name"])
                ],
                "runtime": {
                    "docker": tool["container"]["image"],
                    **{
                        key: value
                        for key, value in tool.get("runtime", {}).items()
                        if key in {"cpu", "memory_gb", "disk_gb"}
                    },
                },
            }
        )

    workflow_inputs = [
        {"name": node["id"], "type": _type(node["port"])}
        for node in sorted(nodes.values(), key=lambda item: item["id"])
        if node["type"] == "workflow_input"
    ]
    calls = []
    for node_id in call_order:
        node = nodes[node_id]
        is_subworkflow = node["type"] == "subworkflow"
        if is_subworkflow:
            ref = node["subworkflow_ref"]
            item = subworkflows[(ref["slug"], ref["version"], ref["digest"])]
            callable_inputs = item["interface_contract"]["inputs"]
        else:
            tool = tools[node["tool_ref"]["digest"]]
            callable_inputs = tool["inputs"]
        bindings: dict[str, Any] = {}
        incoming = {
            edge["target"]["port"]: edge["source"]
            for edge in edges
            if edge["target"]["node_id"] == node_id
        }
        for port in sorted(callable_inputs, key=lambda item: item["name"]):
            source = incoming.get(port["name"])
            if source:
                source_node = nodes[source["node_id"]]
                bindings[port["name"]] = (
                    {"kind": "workflow_input_ref", "name": source_node["id"]}
                    if source_node["type"] == "workflow_input"
                    else {
                        "kind": "call_output_ref",
                        "call": source_node["id"],
                        "output": source["port"],
                    }
                )
            elif port["name"] in node.get("parameter_values", {}):
                bindings[port["name"]] = _literal(node["parameter_values"][port["name"]], port["wdl_type"])
            elif "default" in port:
                bindings[port["name"]] = _literal(port["default"], port["wdl_type"])
        if is_subworkflow:
            calls.append(
                {
                    "alias": node_id,
                    "task": item["workflow_name"],
                    "namespace": item["namespace"],
                    "bindings": bindings,
                }
            )
        else:
            calls.append({"alias": node_id, "task": tool["id"], "bindings": bindings})

    workflow_outputs = []
    for node in sorted(nodes.values(), key=lambda item: item["id"]):
        if node["type"] != "workflow_output":
            continue
        edge = next(edge for edge in edges if edge["target"]["node_id"] == node["id"])
        source = edge["source"]
        source_node = nodes[source["node_id"]]
        expression = (
            {"kind": "workflow_input_ref", "name": source_node["id"]}
            if source_node["type"] == "workflow_input"
            else {"kind": "call_output_ref", "call": source_node["id"], "output": source["port"]}
        )
        workflow_outputs.append({"name": node["id"], "type": _type(node["port"]), "expression": expression})

    return {
        "ir_version": "1.0.0",
        "source": {
            "workflow_id": graph["id"],
            "workflow_schema_version": graph["schema_version"],
            "workflow_semantic_digest": semantic_digest(graph),
            "tool_digests": sorted(used_tools),
        },
        "target": graph["target"],
        "imports": [
            {
                "path": item["artifact_path"],
                "namespace": item["namespace"],
                "slug": item["slug"],
                "version": item["version"],
                "semantic_digest": item["semantic_digest"],
            }
            for item in sorted(
                subworkflows.values(), key=lambda value: (value["slug"], value["version"])
            )
        ],
        "tasks": tasks,
        "workflow": {
            "name": graph["id"],
            "inputs": workflow_inputs,
            "calls": calls,
            "outputs": workflow_outputs,
        },
    }


def _wdl_type(item: dict[str, Any]) -> str:
    return item["type"]["wdl_type"] + ("?" if item["type"].get("optional") else "")


def _wdl_literal(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_wdl(ir: dict[str, Any]) -> str:
    lines = ["version 1.0", ""]
    for item in ir.get("imports", []):
        lines.append(f'import "{item["path"]}" as {item["namespace"]}')
    if ir.get("imports"):
        lines.append("")
    for task in ir["tasks"]:
        lines += [f"task {task['name']} {{", "  input {"]
        for item in task["inputs"]:
            rendered_type = item["type"]["wdl_type"] if "default" in item else _wdl_type(item)
            line = f"    {rendered_type} {item['name']}"
            if "default" in item:
                line += f" = {_wdl_literal(item['default']['value'])}"
            lines.append(line)
        lines += ["  }", "", "  command <<<"]
        if task["command"]["strict_mode"]:
            lines.append("    set -euo pipefail")
        command = "".join(
            segment["value"] if segment["kind"] == "literal" else f"~{{{segment['name']}}}"
            for segment in task["command"]["segments"]
        ).rstrip("\n")
        lines.extend(f"    {line}" for line in command.splitlines())
        lines += ["  >>>", "", "  output {"]
        for item in task["outputs"]:
            lines.append(f"    {_wdl_type(item)} {item['name']} = {_wdl_literal(item['expression']['value'])}")
        lines += ["  }", "", "  runtime {", f"    docker: {_wdl_literal(task['runtime']['docker'])}"]
        if "cpu" in task["runtime"]:
            lines.append(f"    cpu: {task['runtime']['cpu']}")
        if "memory_gb" in task["runtime"]:
            lines.append(f"    memory: {_wdl_literal(str(task['runtime']['memory_gb']) + ' GB')}")
        lines += ["  }", "}", ""]

    workflow = ir["workflow"]
    lines += [f"workflow {workflow['name']} {{", "  input {"]
    for item in workflow["inputs"]:
        lines.append(f"    {_wdl_type(item)} {item['name']}")
    lines += ["  }", ""]
    for call in workflow["calls"]:
        target = (
            f"{call['namespace']}.{call['task']}"
            if call.get("namespace")
            else call["task"]
        )
        lines += [f"  call {target} as {call['alias']} {{", "    input:"]
        bindings = list(call["bindings"].items())
        for index, (name, expression) in enumerate(bindings):
            if expression["kind"] == "workflow_input_ref":
                value = expression["name"]
            elif expression["kind"] == "call_output_ref":
                value = f"{expression['call']}.{expression['output']}"
            else:
                value = _wdl_literal(expression["value"])
            lines.append(f"      {name} = {value}{',' if index < len(bindings) - 1 else ''}")
        lines += ["  }", ""]
    lines += ["  output {"]
    for item in workflow["outputs"]:
        expression = item["expression"]
        value = expression["name"] if expression["kind"] == "workflow_input_ref" else f"{expression['call']}.{expression['output']}"
        lines.append(f"    {_wdl_type(item)} {item['name']} = {value}")
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def _artifact(name: str, media_type: str, content: str) -> dict[str, str]:
    return {
        "name": name,
        "media_type": media_type,
        "digest": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
    }


def validate_wdl(
    wdl: str, dependencies: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Run miniwdl's parser/type checker without executing the workflow."""
    executable = shutil.which("miniwdl")
    if executable is None:
        return [
            diagnostic(
                "SYS003",
                "system",
                "miniwdl is unavailable; generated WDL could not be syntax-checked.",
            )
        ]

    with tempfile.TemporaryDirectory(prefix="bioworkflow-wdl-") as temp_dir:
        path = Path(temp_dir) / "workflow.wdl"
        path.write_text(wdl, encoding="utf-8")
        for name, content in (dependencies or {}).items():
            dependency_path = Path(temp_dir) / name
            dependency_path.parent.mkdir(parents=True, exist_ok=True)
            dependency_path.write_text(content, encoding="utf-8")
        result = subprocess.run(
            [executable, "check", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    if result.returncode == 0:
        return []

    details = (result.stderr or result.stdout).strip()
    if len(details) > 4000:
        details = details[:3997] + "..."
    return [
        diagnostic(
            "WDL001",
            "wdl_validation",
            f"miniwdl rejected the generated WDL: {details or 'unknown syntax/type error'}",
        )
    ]


def compile_workflow(
    graph: dict[str, Any],
    tool_specs: list[dict[str, Any]],
    subworkflow_specs: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    validation, call_order = validate_workflow_graph(
        graph, tool_specs, subworkflow_specs
    )
    if validation["status"] != "valid":
        return validation, []
    ir = lower_to_ir(graph, tool_specs, call_order, subworkflow_specs)
    wdl = render_wdl(ir)
    dependency_contents = {
        item["artifact_path"]: item["wdl"]
        for item in (subworkflow_specs or [])
    }
    wdl_diagnostics = (
        validate_wdl(wdl, dependency_contents)
        if dependency_contents
        else validate_wdl(wdl)
    )
    if wdl_diagnostics:
        return report(
            wdl_diagnostics,
            source={
                "kind": "workflow_graph",
                "id": graph["id"],
                "digest": semantic_digest(graph),
            },
        ), []
    inputs = {
        f"{ir['workflow']['name']}.{item['name']}": f"/path/to/{item['name']}"
        for item in ir["workflow"]["inputs"]
    }
    manifest = {
        "manifest_version": "1.0.0",
        "compiler_contract": "phase1",
        "target": ir["target"],
        "workflow": {
            "id": graph["id"],
            "graph_schema_version": graph["schema_version"],
            "semantic_digest": semantic_digest(graph),
        },
        "ir": {"version": ir["ir_version"], "digest": canonical_digest(ir)},
        "tools": [
            {
                "id": tool["id"],
                "tool_version": tool["tool_version"],
                "tool_spec_schema_version": tool["schema_version"],
                "digest": canonical_digest(tool),
                "container": tool["container"]["image"],
            }
            for tool in sorted(tool_specs, key=lambda item: item["id"])
            if canonical_digest(tool) in ir["source"]["tool_digests"]
        ],
        "subworkflows": [
            {
                "slug": item["slug"],
                "version": item["version"],
                "semantic_digest": item["semantic_digest"],
                "path": item["artifact_path"],
            }
            for item in (subworkflow_specs or [])
        ],
        "artifacts": [
            {"path": "compiler-ir.json", "media_type": "application/json"},
            {"path": "workflow.wdl", "media_type": "application/wdl"},
            {"path": "inputs.template.json", "media_type": "application/json"},
            *[
                {"path": item["artifact_path"], "media_type": "application/wdl"}
                for item in (subworkflow_specs or [])
            ],
        ],
    }
    return validation, [
        _artifact("compiler-ir.json", "application/json", json.dumps(ir, ensure_ascii=False, indent=2) + "\n"),
        _artifact("workflow.wdl", "application/wdl", wdl),
        _artifact("inputs.template.json", "application/json", json.dumps(inputs, ensure_ascii=False, indent=2) + "\n"),
        _artifact("compile-manifest.json", "application/json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"),
        *[
            _artifact(item["artifact_path"], "application/wdl", item["wdl"])
            for item in (subworkflow_specs or [])
        ],
    ]

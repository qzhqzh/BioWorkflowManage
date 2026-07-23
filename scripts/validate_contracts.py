#!/usr/bin/env python3
"""Validate Phase 1 schemas, diagnostics, and golden fixtures.

This script intentionally depends only on jsonschema plus the Python standard
library. WDL syntax validation is executed separately by miniwdl in CI.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
FIXTURE = ROOT / "examples" / "phase1-fastp"
VALIDATION_FIXTURE = ROOT / "examples" / "validation"

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


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def canonical_digest(document: Any) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def validate_document(schema: dict[str, Any], document: Any, name: str) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if not errors:
        return

    messages: list[str] = []
    for error in errors:
        path = "/" + "/".join(str(part) for part in error.absolute_path)
        messages.append(f"{name}{path}: {error.message}")
    raise AssertionError("\n".join(messages))


def assert_unique(values: Iterable[str], label: str) -> None:
    materialized = list(values)
    if len(materialized) != len(set(materialized)):
        raise AssertionError(f"Duplicate {label}: {materialized}")


def assert_sorted(values: Iterable[str], label: str) -> None:
    materialized = list(values)
    if materialized != sorted(materialized):
        raise AssertionError(f"{label} must be sorted: {materialized}")


def contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)
    return False


def diagnostic_sort_key(diagnostic: dict[str, Any]) -> tuple[Any, ...]:
    location = diagnostic.get("location", {})
    return (
        STAGE_ORDER[diagnostic["stage"]],
        0 if diagnostic["severity"] == "error" else 1,
        diagnostic.get("path", ""),
        location.get("node_id", ""),
        location.get("edge_id", ""),
        location.get("port", ""),
        diagnostic["code"],
        diagnostic["message"],
    )


def validate_error_catalog(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if catalog.get("catalog_version") != "1.0.0":
        raise AssertionError("Unsupported error catalog version")

    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        raise AssertionError("Error catalog must contain entries")

    codes: list[str] = []
    index: dict[str, dict[str, Any]] = {}
    for entry in entries:
        expected_keys = {"code", "stage", "severity", "title"}
        if set(entry) != expected_keys:
            raise AssertionError(f"Invalid catalog entry shape: {entry}")
        code = entry["code"]
        if entry["stage"] not in STAGE_ORDER:
            raise AssertionError(f"Unknown diagnostic stage for {code}")
        if entry["severity"] not in {"error", "warning"}:
            raise AssertionError(f"Unknown diagnostic severity for {code}")
        if not isinstance(entry["title"], str) or not entry["title"]:
            raise AssertionError(f"Missing catalog title for {code}")
        codes.append(code)
        index[code] = entry

    assert_unique(codes, "diagnostic code")
    return index


def validate_report_invariants(
    report: dict[str, Any], catalog: dict[str, dict[str, Any]]
) -> None:
    diagnostics = report["diagnostics"]
    error_count = sum(item["severity"] == "error" for item in diagnostics)
    warning_count = sum(item["severity"] == "warning" for item in diagnostics)

    if report["summary"] != {
        "error_count": error_count,
        "warning_count": warning_count,
    }:
        raise AssertionError("Validation summary does not match diagnostics")

    expected_status = "invalid" if error_count else "valid"
    if report["status"] != expected_status:
        raise AssertionError(
            f"Validation status mismatch: expected={expected_status}, "
            f"actual={report['status']}"
        )

    if diagnostics != sorted(diagnostics, key=diagnostic_sort_key):
        raise AssertionError("Diagnostics are not in deterministic order")

    for diagnostic in diagnostics:
        code = diagnostic["code"]
        if code not in catalog:
            raise AssertionError(f"Diagnostic code is absent from catalog: {code}")
        catalog_entry = catalog[code]
        if diagnostic["stage"] != catalog_entry["stage"]:
            raise AssertionError(f"Diagnostic stage differs from catalog for {code}")
        if diagnostic["severity"] != catalog_entry["severity"]:
            raise AssertionError(f"Diagnostic severity differs from catalog for {code}")


def validate_ir_invariants(
    ir: dict[str, Any], tool_digest: str, graph_digest: str
) -> None:
    source = ir["source"]
    if source["workflow_semantic_digest"] != graph_digest:
        raise AssertionError("IR references the wrong Workflow Graph digest")
    if source["tool_digests"] != [tool_digest]:
        raise AssertionError("IR ToolSpec digest set is inconsistent")
    assert_sorted(source["tool_digests"], "IR tool digests")

    if contains_key(ir, "layout"):
        raise AssertionError("Compiler IR must not contain UI layout")

    tasks = ir["tasks"]
    assert_unique((task["name"] for task in tasks), "IR task name")
    assert_sorted((task["name"] for task in tasks), "IR task names")
    for task in tasks:
        if task["source_tool"]["digest"] not in source["tool_digests"]:
            raise AssertionError(f"Task {task['name']} references an unknown ToolSpec digest")
        assert_unique((item["name"] for item in task["inputs"]), "task input")
        assert_sorted((item["name"] for item in task["inputs"]), "task inputs")
        assert_unique((item["name"] for item in task["outputs"]), "task output")
        assert_sorted((item["name"] for item in task["outputs"]), "task outputs")

        declared_inputs = {item["name"] for item in task["inputs"]}
        for segment in task["command"]["segments"]:
            if segment["kind"] == "input_ref" and segment["name"] not in declared_inputs:
                raise AssertionError(
                    f"Command in task {task['name']} references unknown input "
                    f"{segment['name']}"
                )

    workflow = ir["workflow"]
    assert_unique((item["name"] for item in workflow["inputs"]), "workflow input")
    assert_sorted((item["name"] for item in workflow["inputs"]), "workflow inputs")
    assert_unique((item["alias"] for item in workflow["calls"]), "call alias")
    assert_unique((item["name"] for item in workflow["outputs"]), "workflow output")
    assert_sorted((item["name"] for item in workflow["outputs"]), "workflow outputs")

    task_names = {task["name"] for task in tasks}
    workflow_inputs = {item["name"] for item in workflow["inputs"]}
    calls_seen: set[str] = set()
    call_outputs = {
        task["name"]: {item["name"] for item in task["outputs"]} for task in tasks
    }

    for call in workflow["calls"]:
        if call["task"] not in task_names:
            raise AssertionError(f"Call {call['alias']} references unknown task")
        if list(call["bindings"]) != sorted(call["bindings"]):
            raise AssertionError(f"Bindings for {call['alias']} must be sorted")
        for expression in call["bindings"].values():
            if expression["kind"] == "workflow_input_ref":
                if expression["name"] not in workflow_inputs:
                    raise AssertionError("Binding references unknown workflow input")
            elif expression["kind"] == "call_output_ref":
                if expression["call"] not in calls_seen:
                    raise AssertionError("Call output reference violates topology")
        calls_seen.add(call["alias"])

    call_to_task = {call["alias"]: call["task"] for call in workflow["calls"]}
    for output in workflow["outputs"]:
        expression = output["expression"]
        if expression["kind"] == "workflow_input_ref":
            if expression["name"] not in workflow_inputs:
                raise AssertionError("Workflow output references unknown workflow input")
        else:
            call_alias = expression["call"]
            if call_alias not in call_to_task:
                raise AssertionError("Workflow output references unknown call")
            task_name = call_to_task[call_alias]
            if expression["output"] not in call_outputs[task_name]:
                raise AssertionError("Workflow output references unknown task output")


def main() -> None:
    tool_schema = load_json(SCHEMAS / "tool-spec.schema.json")
    graph_schema = load_json(SCHEMAS / "workflow-graph.schema.json")
    ir_schema = load_json(SCHEMAS / "compiler-ir.schema.json")
    report_schema = load_json(SCHEMAS / "validation-report.schema.json")

    for schema in (tool_schema, graph_schema, ir_schema, report_schema):
        Draft202012Validator.check_schema(schema)

    tool = load_json(FIXTURE / "tool-fastp.json")
    graph = load_json(FIXTURE / "workflow-graph.json")
    ir = load_json(FIXTURE / "expected" / "compiler-ir.json")
    manifest = load_json(FIXTURE / "expected" / "compile-manifest.json")
    load_json(FIXTURE / "expected" / "inputs.template.json")

    validation_report = load_json(
        VALIDATION_FIXTURE / "semantic-mismatch-report.json"
    )
    error_catalog = validate_error_catalog(load_json(SCHEMAS / "error-catalog.json"))

    validate_document(tool_schema, tool, "tool-fastp.json")
    validate_document(graph_schema, graph, "workflow-graph.json")
    validate_document(ir_schema, ir, "compiler-ir.json")
    validate_document(
        report_schema, validation_report, "semantic-mismatch-report.json"
    )

    tool_digest = canonical_digest(tool)
    tool_nodes = [node for node in graph["nodes"] if node["type"] == "tool"]
    if len(tool_nodes) != 1:
        raise AssertionError("The first fixture must contain exactly one tool node")

    graph_tool_digest = tool_nodes[0]["tool_ref"]["digest"]
    if graph_tool_digest != tool_digest:
        raise AssertionError(
            f"ToolRef digest mismatch: graph={graph_tool_digest}, actual={tool_digest}"
        )

    manifest_tool_digest = manifest["tools"][0]["digest"]
    if manifest_tool_digest != tool_digest:
        raise AssertionError(
            f"Manifest ToolSpec digest mismatch: manifest={manifest_tool_digest}, "
            f"actual={tool_digest}"
        )

    semantic_graph = {
        key: value
        for key, value in graph.items()
        if key not in {"layout", "metadata"}
    }
    semantic_digest = canonical_digest(semantic_graph)
    manifest_graph_digest = manifest["workflow"]["semantic_digest"]
    if manifest_graph_digest != semantic_digest:
        raise AssertionError(
            f"Workflow semantic digest mismatch: manifest={manifest_graph_digest}, "
            f"actual={semantic_digest}"
        )

    validate_ir_invariants(ir, tool_digest, semantic_digest)
    ir_digest = canonical_digest(ir)
    if manifest["ir"]["digest"] != ir_digest:
        raise AssertionError(
            f"Compiler IR digest mismatch: manifest={manifest['ir']['digest']}, "
            f"actual={ir_digest}"
        )
    if manifest["ir"]["version"] != ir["ir_version"]:
        raise AssertionError("Compiler IR version mismatch in manifest")

    artifact_paths = {artifact["path"] for artifact in manifest["artifacts"]}
    required_artifacts = {"compiler-ir.json", "workflow.wdl", "inputs.template.json"}
    if not required_artifacts.issubset(artifact_paths):
        raise AssertionError("Compile manifest is missing required Phase 1 artifacts")

    validate_report_invariants(validation_report, error_catalog)

    wdl_path = FIXTURE / "expected" / "workflow.wdl"
    wdl = wdl_path.read_text(encoding="utf-8")
    required_fragments = (
        "version 1.0",
        "task fastp",
        "workflow fastp_demo",
        "call fastp as fastp_1",
    )
    for fragment in required_fragments:
        if fragment not in wdl:
            raise AssertionError(f"Missing WDL fragment: {fragment}")

    print("Phase 1 ToolSpec, Graph, IR, diagnostics, and golden fixtures are consistent.")
    print(f"ToolSpec digest: {tool_digest}")
    print(f"Workflow semantic digest: {semantic_digest}")
    print(f"Compiler IR digest: {ir_digest}")
    print(f"Diagnostic catalog entries: {len(error_catalog)}")


if __name__ == "__main__":
    main()

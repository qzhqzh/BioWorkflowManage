#!/usr/bin/env python3
"""Validate Phase 1 schemas, diagnostics, and golden fixtures.

The script intentionally depends only on jsonschema plus the Python standard
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

STAGES = (
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
STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}


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


def graph_semantic_document(graph: dict[str, Any]) -> dict[str, Any]:
    """Return the Phase 1 semantic canonical form of a Workflow Graph."""
    semantic = {
        key: value
        for key, value in graph.items()
        if key not in {"layout", "metadata"}
    }
    semantic["nodes"] = sorted(semantic["nodes"], key=lambda item: item["id"])
    semantic["edges"] = sorted(semantic["edges"], key=lambda item: item["id"])
    return semantic


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
    items = list(values)
    if len(items) != len(set(items)):
        raise AssertionError(f"Duplicate {label}: {items}")


def assert_sorted(values: Iterable[str], label: str) -> None:
    items = list(values)
    if items != sorted(items):
        raise AssertionError(f"{label} must be sorted: {items}")


def contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)
    return False


def validate_error_catalog(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if catalog.get("catalog_version") != "1.0.0":
        raise AssertionError("Unsupported error catalog version")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        raise AssertionError("Error catalog must contain entries")

    index: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if set(entry) != {"code", "stage", "severity", "title"}:
            raise AssertionError(f"Invalid catalog entry shape: {entry}")
        code = entry["code"]
        if code in index:
            raise AssertionError(f"Duplicate diagnostic code: {code}")
        if entry["stage"] not in STAGE_ORDER:
            raise AssertionError(f"Unknown diagnostic stage for {code}")
        if entry["severity"] not in {"error", "warning"}:
            raise AssertionError(f"Unknown diagnostic severity for {code}")
        if not entry["title"]:
            raise AssertionError(f"Missing catalog title for {code}")
        index[code] = entry
    return index


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


def validate_report(
    report: dict[str, Any], catalog: dict[str, dict[str, Any]]
) -> None:
    diagnostics = report["diagnostics"]
    errors = sum(item["severity"] == "error" for item in diagnostics)
    warnings = sum(item["severity"] == "warning" for item in diagnostics)
    if report["summary"] != {"error_count": errors, "warning_count": warnings}:
        raise AssertionError("Validation summary does not match diagnostics")
    expected_status = "invalid" if errors else "valid"
    if report["status"] != expected_status:
        raise AssertionError("Validation status does not match diagnostics")
    if diagnostics != sorted(diagnostics, key=diagnostic_sort_key):
        raise AssertionError("Diagnostics are not deterministically sorted")

    for diagnostic in diagnostics:
        entry = catalog.get(diagnostic["code"])
        if entry is None:
            raise AssertionError(f"Unknown diagnostic code: {diagnostic['code']}")
        if diagnostic["stage"] != entry["stage"]:
            raise AssertionError(f"Stage differs from catalog: {diagnostic['code']}")
        if diagnostic["severity"] != entry["severity"]:
            raise AssertionError(f"Severity differs from catalog: {diagnostic['code']}")


def validate_ir(
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
    task_outputs: dict[str, set[str]] = {}
    for task in tasks:
        if task["source_tool"]["digest"] not in source["tool_digests"]:
            raise AssertionError(f"Task {task['name']} references unknown ToolSpec")
        assert_sorted((item["name"] for item in task["inputs"]), "task inputs")
        assert_sorted((item["name"] for item in task["outputs"]), "task outputs")
        declared_inputs = {item["name"] for item in task["inputs"]}
        task_outputs[task["name"]] = {item["name"] for item in task["outputs"]}
        for segment in task["command"]["segments"]:
            if segment["kind"] == "input_ref" and segment["name"] not in declared_inputs:
                raise AssertionError("Command segment references unknown task input")

    workflow = ir["workflow"]
    assert_sorted((item["name"] for item in workflow["inputs"]), "workflow inputs")
    assert_sorted((item["name"] for item in workflow["outputs"]), "workflow outputs")
    assert_unique((item["alias"] for item in workflow["calls"]), "call alias")

    workflow_inputs = {item["name"] for item in workflow["inputs"]}
    task_names = {task["name"] for task in tasks}
    calls_seen: set[str] = set()
    call_to_task: dict[str, str] = {}
    for call in workflow["calls"]:
        if call["task"] not in task_names:
            raise AssertionError("Call references unknown task")
        if list(call["bindings"]) != sorted(call["bindings"]):
            raise AssertionError("Call bindings must be sorted")
        for expression in call["bindings"].values():
            if expression["kind"] == "workflow_input_ref":
                if expression["name"] not in workflow_inputs:
                    raise AssertionError("Binding references unknown workflow input")
            elif expression["kind"] == "call_output_ref":
                if expression["call"] not in calls_seen:
                    raise AssertionError("Call output reference violates topology")
        calls_seen.add(call["alias"])
        call_to_task[call["alias"]] = call["task"]

    for output in workflow["outputs"]:
        expression = output["expression"]
        if expression["kind"] == "workflow_input_ref":
            if expression["name"] not in workflow_inputs:
                raise AssertionError("Workflow output references unknown input")
        else:
            call = expression["call"]
            if call not in call_to_task:
                raise AssertionError("Workflow output references unknown call")
            if expression["output"] not in task_outputs[call_to_task[call]]:
                raise AssertionError("Workflow output references unknown task output")


def main() -> None:
    schemas = {
        name: load_json(SCHEMAS / name)
        for name in (
            "tool-spec.schema.json",
            "workflow-graph.schema.json",
            "compiler-ir.schema.json",
            "validation-report.schema.json",
        )
    }
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)

    tool = load_json(FIXTURE / "tool-fastp.json")
    graph = load_json(FIXTURE / "workflow-graph.json")
    ir = load_json(FIXTURE / "expected" / "compiler-ir.json")
    manifest = load_json(FIXTURE / "expected" / "compile-manifest.json")
    load_json(FIXTURE / "expected" / "inputs.template.json")
    report = load_json(VALIDATION_FIXTURE / "semantic-mismatch-report.json")
    catalog = validate_error_catalog(load_json(SCHEMAS / "error-catalog.json"))

    validate_document(schemas["tool-spec.schema.json"], tool, "tool-fastp.json")
    validate_document(schemas["workflow-graph.schema.json"], graph, "workflow-graph.json")
    validate_document(schemas["compiler-ir.schema.json"], ir, "compiler-ir.json")
    validate_document(
        schemas["validation-report.schema.json"],
        report,
        "semantic-mismatch-report.json",
    )

    tool_digest = canonical_digest(tool)
    tool_nodes = [node for node in graph["nodes"] if node["type"] == "tool"]
    if len(tool_nodes) != 1:
        raise AssertionError("The first fixture must contain exactly one tool node")
    if tool_nodes[0]["tool_ref"]["digest"] != tool_digest:
        raise AssertionError("Workflow Graph ToolRef digest mismatch")
    if manifest["tools"][0]["digest"] != tool_digest:
        raise AssertionError("Manifest ToolSpec digest mismatch")

    semantic_digest = canonical_digest(graph_semantic_document(graph))
    if manifest["workflow"]["semantic_digest"] != semantic_digest:
        raise AssertionError("Manifest Workflow semantic digest mismatch")

    validate_ir(ir, tool_digest, semantic_digest)
    ir_digest = canonical_digest(ir)
    if manifest["ir"]["digest"] != ir_digest:
        raise AssertionError("Manifest Compiler IR digest mismatch")
    if manifest["ir"]["version"] != ir["ir_version"]:
        raise AssertionError("Manifest Compiler IR version mismatch")

    artifacts = {artifact["path"] for artifact in manifest["artifacts"]}
    required = {"compiler-ir.json", "workflow.wdl", "inputs.template.json"}
    if not required.issubset(artifacts):
        raise AssertionError("Compile manifest is missing required artifacts")

    validate_report(report, catalog)

    wdl = (FIXTURE / "expected" / "workflow.wdl").read_text(encoding="utf-8")
    for fragment in (
        "version 1.0",
        "task fastp",
        "workflow fastp_demo",
        "call fastp as fastp_1",
    ):
        if fragment not in wdl:
            raise AssertionError(f"Missing WDL fragment: {fragment}")

    print("Phase 1 contracts and golden fixtures are consistent.")
    print(f"ToolSpec digest: {tool_digest}")
    print(f"Workflow semantic digest: {semantic_digest}")
    print(f"Compiler IR digest: {ir_digest}")
    print(f"Diagnostic catalog entries: {len(catalog)}")


if __name__ == "__main__":
    main()

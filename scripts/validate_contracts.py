#!/usr/bin/env python3
"""Validate Phase 1 schemas and golden fixtures.

This script intentionally depends only on jsonschema plus the Python standard
library. WDL syntax validation is executed separately by miniwdl in CI.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
FIXTURE = ROOT / "examples" / "phase1-fastp"


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


def main() -> None:
    tool_schema = load_json(SCHEMAS / "tool-spec.schema.json")
    graph_schema = load_json(SCHEMAS / "workflow-graph.schema.json")

    Draft202012Validator.check_schema(tool_schema)
    Draft202012Validator.check_schema(graph_schema)

    tool = load_json(FIXTURE / "tool-fastp.json")
    graph = load_json(FIXTURE / "workflow-graph.json")
    manifest = load_json(FIXTURE / "expected" / "compile-manifest.json")
    load_json(FIXTURE / "expected" / "inputs.template.json")

    validate_document(tool_schema, tool, "tool-fastp.json")
    validate_document(graph_schema, graph, "workflow-graph.json")

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

    print("Phase 1 contracts and fastp golden fixture are consistent.")
    print(f"ToolSpec digest: {tool_digest}")
    print(f"Workflow semantic digest: {semantic_digest}")


if __name__ == "__main__":
    main()

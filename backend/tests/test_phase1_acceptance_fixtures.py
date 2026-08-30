import json
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from compiler_core.compiler import compile_workflow, lower_to_ir
from compiler_core.validation import canonical_digest, validate_tool_spec


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
FIXTURE = ROOT / "examples" / "phase1-fastp-bwa"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture():
    fixture = load_json(FIXTURE / "fixture.json")
    graph = load_json(FIXTURE / fixture["workflow_graph"])
    tools = [load_json(FIXTURE / path) for path in fixture["tool_specs"]]
    return fixture, graph, tools


def assert_matches_schema(document, schema_name):
    schema = load_json(SCHEMAS / schema_name)
    Draft202012Validator(schema).validate(document)


def test_fastp_bwa_fixture_resolves_complete_tool_bundle():
    fixture, graph, tools = load_fixture()

    assert fixture["scenario"] == "linear"
    assert len(tools) == 2
    for tool in tools:
        assert_matches_schema(tool, "tool-spec.schema.json")
        assert validate_tool_spec(tool)["status"] == "valid"
    assert_matches_schema(graph, "workflow-graph.schema.json")

    tool_digests = {canonical_digest(tool) for tool in tools}
    referenced_digests = {
        node["tool_ref"]["digest"]
        for node in graph["nodes"]
        if node["type"] == "tool"
    }
    assert referenced_digests == tool_digests

    for relative_path in fixture["invalid_workflow_graphs"]:
        assert (FIXTURE / relative_path).is_file()
    for relative_path in fixture["expected_artifacts"]:
        assert (FIXTURE / relative_path).is_file()


@pytest.mark.parametrize(
    ("wdl_type", "required", "default"),
    [
        ("Int", False, "4"),
        ("Boolean", False, 1),
        ("Array[Int]", False, [1, "2"]),
        ("File", False, "/etc/passwd"),
        ("Directory", False, "/tmp/database"),
        ("Int", True, 4),
    ],
)
def test_tool_input_default_must_match_type_and_required_rules(
    wdl_type, required, default
):
    tool = load_json(ROOT / "examples/phase1-fastp/tool-fastp.json")
    invalid = deepcopy(tool)
    invalid["inputs"][2].update(
        {"wdl_type": wdl_type, "required": required, "default": default}
    )

    validation = validate_tool_spec(invalid)

    assert validation["status"] == "invalid"
    assert any(item["code"] == "TS004" for item in validation["diagnostics"])


def test_optional_scalar_null_default_renders_as_optional_wdl():
    tool = load_json(ROOT / "examples/phase1-fastp/tool-fastp.json")
    tool["inputs"][2]["default"] = None
    graph = load_json(ROOT / "examples/phase1-fastp/workflow-graph.json")
    tool_node = next(node for node in graph["nodes"] if node["type"] == "tool")
    tool_node["tool_ref"]["digest"] = canonical_digest(tool)

    validation, artifacts = compile_workflow(graph, [tool])

    assert validation["status"] == "valid"
    wdl = next(item["content"] for item in artifacts if item["name"] == "workflow.wdl")
    assert "Int? threads" in wdl
    assert "threads = None" not in wdl


def test_fastp_bwa_fixture_matches_real_compiler_artifacts():
    fixture, graph, tools = load_fixture()

    validation, artifacts = compile_workflow(graph, tools)
    repeated_validation, repeated_artifacts = compile_workflow(graph, tools)

    assert validation["status"] == "valid"
    assert repeated_validation["status"] == "valid"
    assert artifacts == repeated_artifacts

    generated = {artifact["name"]: artifact["content"] for artifact in artifacts}
    expected = {
        Path(relative_path).name: (FIXTURE / relative_path).read_text(encoding="utf-8")
        for relative_path in fixture["expected_artifacts"]
    }
    assert generated == expected

    ir = json.loads(generated["compiler-ir.json"])
    manifest = json.loads(generated["compile-manifest.json"])
    assert_matches_schema(ir, "compiler-ir.schema.json")
    assert manifest["workflow"]["semantic_digest"] == validation["source"]["digest"]
    assert manifest["ir"] == {
        "version": ir["ir_version"],
        "digest": canonical_digest(ir),
    }
    assert {item["digest"] for item in manifest["tools"]} == {
        canonical_digest(tool) for tool in tools
    }


def test_fastp_bwa_semantic_mismatch_fixture_is_rejected():
    fixture, _, tools = load_fixture()
    invalid_graph = load_json(FIXTURE / fixture["invalid_workflow_graphs"][0])

    assert_matches_schema(invalid_graph, "workflow-graph.schema.json")
    validation, artifacts = compile_workflow(invalid_graph, tools)

    assert validation["status"] == "invalid"
    assert artifacts == []
    assert validation["summary"] == {"error_count": 1, "warning_count": 0}
    assert validation["diagnostics"] == [
        {
            "code": "WG013",
            "stage": "type",
            "severity": "error",
            "message": "Semantic type mismatch.",
            "location": {"edge_id": "edge_reads_1_fastp"},
        }
    ]


def test_compiler_ir_schema_covers_real_subworkflow_lowering():
    digest = "sha256:" + "1" * 64
    contract = {
        "contract_version": "1.0.0",
        "inputs": [
            {
                "name": "reads",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r1",
                "required": True,
            }
        ],
        "outputs": [
            {
                "name": "clean_reads",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r1",
            }
        ],
    }
    graph = {
        "schema_version": "1.0.0",
        "id": "parent_workflow",
        "name": "Parent workflow",
        "target": {
            "language": "wdl",
            "version": "1.0",
            "profile": "miniwdl-compatible",
        },
        "nodes": [
            {
                "id": "input_reads",
                "type": "workflow_input",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "bio.fastq.gz.r1",
                    "required": True,
                },
            },
            {
                "id": "cleanup_1",
                "type": "subworkflow",
                "subworkflow_ref": {
                    "slug": "read_cleanup",
                    "version": 1,
                    "digest": digest,
                },
                "interface_contract": contract,
            },
            {
                "id": "output_clean_reads",
                "type": "workflow_output",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "bio.fastq.gz.r1",
                },
            },
        ],
        "edges": [
            {
                "id": "edge_input_cleanup",
                "source": {"node_id": "input_reads", "port": "value"},
                "target": {"node_id": "cleanup_1", "port": "reads"},
            },
            {
                "id": "edge_cleanup_output",
                "source": {"node_id": "cleanup_1", "port": "clean_reads"},
                "target": {"node_id": "output_clean_reads", "port": "value"},
            },
        ],
    }
    subworkflow = {
        "slug": "read_cleanup",
        "version": 1,
        "semantic_digest": digest,
        "interface_contract": contract,
        "workflow_name": "read_cleanup",
        "namespace": "read_cleanup_v1",
        "artifact_path": "read_cleanup.v1.wdl",
    }

    ir = lower_to_ir(graph, [], ["cleanup_1"], [subworkflow])

    assert ir["imports"] == [
        {
            "path": "read_cleanup.v1.wdl",
            "namespace": "read_cleanup_v1",
            "slug": "read_cleanup",
            "version": 1,
            "semantic_digest": digest,
        }
    ]
    assert ir["workflow"]["calls"][0]["namespace"] == "read_cleanup_v1"
    assert_matches_schema(ir, "compiler-ir.schema.json")

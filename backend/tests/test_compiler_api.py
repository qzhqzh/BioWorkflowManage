import json
from copy import deepcopy
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from compiler_core.compiler import compile_workflow, validate_wdl
from compiler_core.validation import semantic_digest, validate_workflow_graph
from workflows.models import (
    ToolVersion,
    WDLRevision,
    WorkflowDocument,
    WorkflowVersion,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "examples" / "phase1-fastp"
CHAIN_FIXTURE = ROOT / "examples" / "phase1-fastp-bwa"


def fixture():
    graph = json.loads((FIXTURE / "workflow-graph.json").read_text(encoding="utf-8"))
    tool = json.loads((FIXTURE / "tool-fastp.json").read_text(encoding="utf-8"))
    return graph, tool


def chain_fixture():
    graph = json.loads((CHAIN_FIXTURE / "workflow-graph.json").read_text(encoding="utf-8"))
    tools = [
        json.loads((FIXTURE / "tool-fastp.json").read_text(encoding="utf-8")),
        json.loads((CHAIN_FIXTURE / "tool-bwa-mem.json").read_text(encoding="utf-8")),
    ]
    return graph, tools


def test_compiler_matches_golden_wdl_and_ignores_layout():
    graph, tool = fixture()
    validation, artifacts = compile_workflow(graph, [tool])
    assert validation["status"] == "valid"
    generated = next(item["content"] for item in artifacts if item["name"] == "workflow.wdl")
    assert generated == (FIXTURE / "expected" / "workflow.wdl").read_text(encoding="utf-8")

    original_digest = semantic_digest(graph)
    graph["layout"]["nodes"]["fastp_1"] = {"x": 999, "y": 123}
    assert semantic_digest(graph) == original_digest


def test_fastp_bwa_chain_is_a_valid_dag_and_wdl_passes_miniwdl():
    graph, tools = chain_fixture()
    validation, artifacts = compile_workflow(graph, tools)

    assert validation["status"] == "valid"
    wdl = next(item["content"] for item in artifacts if item["name"] == "workflow.wdl")
    assert "call fastp as fastp_1" in wdl
    assert "call bwa_mem as bwa_mem_1" in wdl
    assert "reads_1 = fastp_1.clean_reads_1" in wdl
    assert validate_wdl(wdl) == []


def test_two_tool_cycle_is_rejected_with_wg014():
    graph, tools = chain_fixture()
    cyclic = deepcopy(graph)
    cyclic["edges"] = [
        edge for edge in cyclic["edges"] if edge["id"] != "edge_reads_1_fastp"
    ]
    cyclic["edges"].append(
        {
            "id": "edge_bwa_fastp_cycle",
            "source": {"node_id": "bwa_mem_1", "port": "aligned_bam"},
            "target": {"node_id": "fastp_1", "port": "reads_1"},
        }
    )

    validation, call_order = validate_workflow_graph(cyclic, tools)

    assert call_order == []
    assert validation["status"] == "invalid"
    assert "WG014" in {item["code"] for item in validation["diagnostics"]}


def test_wdl_validation_failure_blocks_artifacts(monkeypatch):
    graph, tools = chain_fixture()
    monkeypatch.setattr(
        "compiler_core.compiler.validate_wdl",
        lambda _wdl: [
            {
                "code": "WDL001",
                "stage": "wdl_validation",
                "severity": "error",
                "message": "synthetic syntax failure",
            }
        ],
    )

    validation, artifacts = compile_workflow(graph, tools)

    assert validation["status"] == "invalid"
    assert validation["diagnostics"][0]["code"] == "WDL001"
    assert artifacts == []


@pytest.mark.django_db
def test_editor_persistence_and_compilation_history():
    graph, tool = fixture()
    client = APIClient()
    saved = client.put(
        "/api/v1/editor/workflows/fastp_demo",
        {
            "name": graph["name"],
            "workflow_graph": graph,
            "tool_specs": [tool],
            "editor_document": {"nodes": []},
        },
        format="json",
    )
    assert saved.status_code == 200

    compiled = client.post(
        "/api/v1/compilations",
        {"request_version": "1.0.0", "workflow_graph": graph, "tool_specs": [tool]},
        format="json",
    )
    assert compiled.status_code == 201
    assert compiled.data["status"] == "succeeded"
    assert len(compiled.data["artifacts"]) == 4

    history = client.get("/api/v1/editor/workflows/fastp_demo/compilations")
    assert history.status_code == 200
    assert history.data["results"][0]["status"] == "succeeded"
    generated_wdl = WDLRevision.objects.get()
    assert generated_wdl.source == "system"
    assert generated_wdl.content.startswith("version 1.0")


@pytest.mark.django_db
def test_tool_versions_are_immutable_and_idempotent():
    _, tool = fixture()
    client = APIClient()

    created = client.post(
        "/api/v1/tools/fastp/versions",
        {"tool_spec": tool},
        format="json",
    )
    assert created.status_code == 201
    assert created.data["version"] == "0.23.4"

    repeated = client.post(
        "/api/v1/tools/fastp/versions",
        {"tool_spec": tool},
        format="json",
    )
    assert repeated.status_code == 200
    assert ToolVersion.objects.count() == 1

    changed = {**tool, "description": "changed"}
    conflict = client.post(
        "/api/v1/tools/fastp/versions",
        {"tool_spec": changed},
        format="json",
    )
    assert conflict.status_code == 409

    detail = client.get("/api/v1/tools/fastp/versions/0.23.4")
    assert detail.status_code == 200
    assert detail.data["tool_spec"] == tool

    snapshot = ToolVersion.objects.get()
    snapshot.name = "mutated"
    with pytest.raises(ValidationError):
        snapshot.save()


@pytest.mark.django_db
def test_workflow_publish_creates_immutable_versions_and_links_compilation():
    graph, tool = fixture()
    client = APIClient()
    saved = client.put(
        "/api/v1/editor/workflows/fastp_demo",
        {
            "name": graph["name"],
            "workflow_graph": graph,
            "tool_specs": [tool],
            "editor_document": {"nodes": []},
        },
        format="json",
    )
    assert saved.status_code == 200

    first = client.post(
        "/api/v1/editor/workflows/fastp_demo/versions", {}, format="json"
    )
    second = client.post(
        "/api/v1/editor/workflows/fastp_demo/versions", {}, format="json"
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert [first.data["version"], second.data["version"]] == [1, 2]

    versions = client.get("/api/v1/editor/workflows/fastp_demo/versions")
    assert versions.status_code == 200
    assert [item["version"] for item in versions.data["results"]] == [2, 1]

    detail = client.get("/api/v1/editor/workflows/fastp_demo/versions/1")
    assert detail.status_code == 200
    assert detail.data["workflow_graph"] == graph

    compiled = client.post(
        "/api/v1/compilations",
        {
            "workflow_graph": graph,
            "tool_specs": [tool],
            "workflow_version": 1,
        },
        format="json",
    )
    assert compiled.status_code == 201
    history = client.get("/api/v1/editor/workflows/fastp_demo/compilations")
    assert history.data["results"][0]["workflow_version"] == 1

    snapshot = WorkflowVersion.objects.get(version=1)
    snapshot.name = "mutated"
    with pytest.raises(ValidationError):
        snapshot.save()


@pytest.mark.django_db
def test_workflow_publish_can_reuse_unchanged_semantic_version():
    graph, tool = fixture()
    client = APIClient()
    document = {
        "name": graph["name"],
        "description": "Stable workflow metadata.",
        "workflow_graph": graph,
        "tool_specs": [tool],
        "editor_document": {"nodes": []},
    }
    saved = client.put(
        "/api/v1/editor/workflows/fastp_demo",
        document,
        format="json",
    )
    assert saved.status_code == 200

    first = client.post(
        "/api/v1/editor/workflows/fastp_demo/versions",
        {},
        format="json",
    )
    assert first.status_code == 201
    assert first.data["reused"] is False

    layout_only_graph = deepcopy(graph)
    layout_only_graph["layout"]["nodes"]["fastp_1"] = {"x": 640, "y": 320}
    layout_saved = client.put(
        "/api/v1/editor/workflows/fastp_demo",
        {
            **document,
            "workflow_graph": layout_only_graph,
            "editor_document": {
                "nodes": [{"id": "fastp_1", "position": {"x": 640, "y": 320}}]
            },
        },
        format="json",
    )
    assert layout_saved.status_code == 200

    reused = client.post(
        "/api/v1/editor/workflows/fastp_demo/versions",
        {"reuse_unchanged": True},
        format="json",
    )
    assert reused.status_code == 200
    assert reused.data["version"] == 1
    assert reused.data["reused"] is True
    assert WorkflowVersion.objects.count() == 1

    metadata_saved = client.put(
        "/api/v1/editor/workflows/fastp_demo",
        {**document, "description": "Changed workflow metadata."},
        format="json",
    )
    assert metadata_saved.status_code == 200
    changed = client.post(
        "/api/v1/editor/workflows/fastp_demo/versions",
        {"reuse_unchanged": True},
        format="json",
    )
    assert changed.status_code == 201
    assert changed.data["version"] == 2
    assert changed.data["reused"] is False


@pytest.mark.django_db
def test_manual_wdl_requires_miniwdl_and_creates_immutable_version():
    graph, tool = fixture()
    client = APIClient()
    client.put(
        "/api/v1/editor/workflows/fastp_demo",
        {
            "name": "Editable workflow",
            "description": "A workflow with manually maintained WDL.",
            "kind": "workflow",
            "workflow_graph": graph,
            "tool_specs": [tool],
            "editor_document": {},
        },
        format="json",
    )
    published = client.post(
        "/api/v1/editor/workflows/fastp_demo/versions", {}, format="json"
    )
    assert published.status_code == 201
    content = (FIXTURE / "expected" / "workflow.wdl").read_text(encoding="utf-8")
    created = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {"content": content, "source": "manual", "workflow_version": 1},
        format="json",
    )
    assert created.status_code == 201
    assert created.data["source"] == "manual"
    assert created.data["version"] == 1
    assert created.data["content"] == content

    rejected = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {"content": "this is not WDL", "source": "manual"},
        format="json",
    )
    assert rejected.status_code == 422
    assert WDLRevision.objects.count() == 1

    listing = client.get(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions"
    )
    assert listing.data["results"][0]["source"] == "manual"
    detail = client.get(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions/1"
    )
    assert detail.data["content"] == content

    revision = WDLRevision.objects.get()
    revision.content = "mutated"
    with pytest.raises(ValidationError):
        revision.save()


@pytest.mark.django_db
def test_subworkflow_publish_extracts_contract_and_parent_pins_exact_version():
    graph, tool = fixture()
    client = APIClient()
    saved = client.put(
        "/api/v1/editor/workflows/read_cleanup",
        {
            "name": "Read cleanup",
            "description": "Reusable FASTQ cleanup.",
            "kind": "subworkflow",
            "workflow_graph": graph,
            "tool_specs": [tool],
            "editor_document": {},
        },
        format="json",
    )
    assert saved.status_code == 200
    published = client.post(
        "/api/v1/editor/workflows/read_cleanup/versions", {}, format="json"
    )
    assert published.status_code == 201
    assert published.data["kind"] == "subworkflow"
    assert [item["name"] for item in published.data["interface_contract"]["inputs"]] == [
        "input_reads_1",
        "input_reads_2",
    ]
    assert [item["name"] for item in published.data["interface_contract"]["outputs"]] == [
        "output_clean_reads_1",
        "output_clean_reads_2",
        "output_html_report",
    ]

    reference = {
        "slug": "read_cleanup",
        "version": 1,
        "digest": published.data["semantic_digest"],
    }
    parent = client.put(
        "/api/v1/editor/workflows/parent",
        {
            "name": "Parent workflow",
            "description": "Uses a fixed child contract.",
            "kind": "workflow",
            "workflow_graph": graph,
            "tool_specs": [tool],
            "editor_document": {},
            "subworkflow_references": [reference],
        },
        format="json",
    )
    assert parent.status_code == 200
    parent_version = client.post(
        "/api/v1/editor/workflows/parent/versions", {}, format="json"
    )
    assert parent_version.status_code == 201
    assert parent_version.data["subworkflow_references"] == [reference]

    invalid = client.put(
        "/api/v1/editor/workflows/parent",
        {
            "name": "Parent workflow",
            "workflow_graph": graph,
            "tool_specs": [tool],
            "editor_document": {},
            "subworkflow_references": [{**reference, "digest": "sha256:wrong"}],
        },
        format="json",
    )
    assert invalid.status_code == 422

    snapshot = WorkflowVersion.objects.get(workflow__slug="read_cleanup")
    assert snapshot.interface_contract["contract_version"] == "1.0.0"
    assert WorkflowDocument.objects.get(slug="parent").description == (
        "Uses a fixed child contract."
    )


@pytest.mark.django_db
def test_subworkflow_node_compiles_as_pinned_import_and_dependency_artifact():
    child_graph, tool = fixture()
    client = APIClient()
    assert client.put(
        "/api/v1/editor/workflows/read_cleanup",
        {
            "name": "Read cleanup",
            "kind": "subworkflow",
            "workflow_graph": child_graph,
            "tool_specs": [tool],
            "editor_document": {},
        },
        format="json",
    ).status_code == 200
    published = client.post(
        "/api/v1/editor/workflows/read_cleanup/versions", {}, format="json"
    )
    assert published.status_code == 201
    contract = published.data["interface_contract"]
    parent_graph = {
        "schema_version": "1.0.0",
        "id": "parent_with_cleanup",
        "name": "Parent with cleanup",
        "target": child_graph["target"],
        "nodes": [
            {
                "id": item["name"],
                "type": "workflow_input",
                "port": {
                    "name": "value",
                    "wdl_type": item["wdl_type"],
                    "semantic_type": item["semantic_type"],
                    "required": item["required"],
                },
            }
            for item in contract["inputs"]
        ]
        + [
            {
                "id": "cleanup_1",
                "type": "subworkflow",
                "subworkflow_ref": {
                    "slug": "read_cleanup",
                    "version": 1,
                    "digest": published.data["semantic_digest"],
                },
                "interface_contract": contract,
            }
        ]
        + [
            {
                "id": item["name"],
                "type": "workflow_output",
                "port": {
                    "name": "value",
                    "wdl_type": item["wdl_type"],
                    "semantic_type": item["semantic_type"],
                },
            }
            for item in contract["outputs"]
        ],
        "edges": [
            {
                "id": f"edge_{item['name']}_cleanup",
                "source": {"node_id": item["name"], "port": "value"},
                "target": {"node_id": "cleanup_1", "port": item["name"]},
            }
            for item in contract["inputs"]
        ]
        + [
            {
                "id": f"edge_cleanup_{item['name']}",
                "source": {"node_id": "cleanup_1", "port": item["name"]},
                "target": {"node_id": item["name"], "port": "value"},
            }
            for item in contract["outputs"]
        ],
    }
    compiled = client.post(
        "/api/v1/compilations",
        {"workflow_graph": parent_graph, "tool_specs": []},
        format="json",
    )
    assert compiled.status_code == 201
    assert compiled.data["status"] == "succeeded"
    names = {item["name"] for item in compiled.data["artifacts"]}
    assert "read_cleanup.v1.wdl" in names
    parent_wdl = next(
        item["content"]
        for item in compiled.data["artifacts"]
        if item["name"] == "workflow.wdl"
    )
    assert 'import "read_cleanup.v1.wdl" as read_cleanup_v1' in parent_wdl
    assert "call read_cleanup_v1.fastp_demo as cleanup_1" in parent_wdl


@pytest.mark.django_db
def test_recursive_subworkflow_dependency_is_rejected():
    graph, tool = fixture()
    document = WorkflowDocument.objects.create(
        slug="recursive_child",
        name="Recursive child",
        kind=WorkflowDocument.Kind.SUBWORKFLOW,
        workflow_graph=graph,
        tool_specs=[tool],
    )
    contract = {
        "contract_version": "1.0.0",
        "inputs": [],
        "outputs": [],
    }
    snapshot = WorkflowVersion.objects.create(
        workflow=document,
        version=1,
        name=document.name,
        kind=WorkflowDocument.Kind.SUBWORKFLOW,
        semantic_digest=semantic_digest(graph),
        workflow_graph=graph,
        editor_document={},
        tool_specs=[tool],
        interface_contract=contract,
    )
    ref = {
        "slug": document.slug,
        "version": snapshot.version,
        "digest": snapshot.semantic_digest,
    }
    recursive_graph = {
        "schema_version": "1.0.0",
        "id": "recursive_child",
        "name": "Recursive child",
        "target": graph["target"],
        "nodes": [
            {
                "id": "recursive_1",
                "type": "subworkflow",
                "subworkflow_ref": ref,
                "interface_contract": contract,
            }
        ],
        "edges": [],
    }
    WorkflowVersion.objects.filter(pk=snapshot.pk).update(
        workflow_graph=recursive_graph
    )
    parent_graph = {
        **recursive_graph,
        "id": "parent_recursive",
        "name": "Parent recursive",
    }
    response = APIClient().post(
        "/api/v1/compilations",
        {"workflow_graph": parent_graph, "tool_specs": []},
        format="json",
    )
    assert response.status_code == 422
    assert response.data["validation"]["diagnostics"][0]["code"] == "WG023"

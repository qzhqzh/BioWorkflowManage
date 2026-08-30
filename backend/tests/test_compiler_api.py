import json
from copy import deepcopy
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from compiler_core.compiler import compile_workflow, validate_wdl
from compiler_core.validation import (
    canonical_digest,
    semantic_digest,
    validate_workflow_graph,
)
from workflows.models import (
    ToolVersion,
    WDLRevision,
    WorkflowDocument,
    WorkflowVersion,
)


pytestmark = pytest.mark.usefixtures("auth_disabled")


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


def test_tool_validation_returns_schema_report_for_non_list_inputs():
    graph, tool = fixture()
    tool["inputs"] = None

    response = APIClient().post(
        "/api/v1/validations/tool-spec",
        {"tool_spec": tool},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["validation"]["status"] == "invalid"
    assert any(
        item["code"] == "SCHEMA001"
        and item["path"] == "/inputs"
        for item in response.data["validation"]["diagnostics"]
    )


def parameter_fixture():
    graph, tool = fixture()
    tool = deepcopy(tool)
    tool["inputs"] = [
        *tool["inputs"],
        {
            "name": "label",
            "wdl_type": "String",
            "semantic_type": "core.string",
            "required": False,
        },
        {
            "name": "threshold",
            "wdl_type": "Float",
            "semantic_type": "core.float",
            "required": False,
        },
        {
            "name": "enabled",
            "wdl_type": "Boolean",
            "semantic_type": "core.boolean",
            "required": False,
        },
        {
            "name": "labels",
            "wdl_type": "Array[String]",
            "semantic_type": "core.string.array",
            "required": False,
        },
        {
            "name": "optional_note",
            "wdl_type": "String",
            "semantic_type": "core.string",
            "required": False,
        },
    ]
    tool_node = next(node for node in graph["nodes"] if node["id"] == "fastp_1")
    tool_node["tool_ref"]["digest"] = canonical_digest(tool)
    return graph, tool


def test_semantic_any_is_only_a_target_file_wildcard():
    graph, tool = fixture()
    source_any = deepcopy(graph)
    source = next(node for node in source_any["nodes"] if node["id"] == "input_reads_1")
    source["port"]["semantic_type"] = "core.file.any"

    validation, _ = validate_workflow_graph(source_any, [tool])

    assert validation["status"] == "invalid"
    assert "WG013" in {item["code"] for item in validation["diagnostics"]}

    target_any = deepcopy(graph)
    target = next(node for node in target_any["nodes"] if node["id"] == "fastp_1")
    target["tool_ref"]["digest"] = canonical_digest(tool)
    tool_with_any = deepcopy(tool)
    reads_1 = next(port for port in tool_with_any["inputs"] if port["name"] == "reads_1")
    reads_1["semantic_type"] = "core.file.any"
    target["tool_ref"]["digest"] = canonical_digest(tool_with_any)

    validation, _ = validate_workflow_graph(target_any, [tool_with_any])

    assert validation["status"] == "valid"


def test_parameter_values_validate_scalars_arrays_and_optional_inputs():
    graph, tool = parameter_fixture()
    tool_node = next(node for node in graph["nodes"] if node["id"] == "fastp_1")
    tool_node["parameter_values"] = {
        "label": "sample-1",
        "threshold": 0.5,
        "enabled": True,
        "labels": ["tumor", "normal"],
    }

    validation, _ = validate_workflow_graph(graph, [tool])

    assert validation["status"] == "valid"


def test_parameter_values_reject_unknown_ports_wrong_types_and_file_paths():
    graph, tool = parameter_fixture()
    tool["inputs"].append(
        {
            "name": "reference",
            "wdl_type": "File",
            "semantic_type": "core.file.any",
            "required": False,
        }
    )
    tool_node = next(node for node in graph["nodes"] if node["id"] == "fastp_1")
    tool_node["tool_ref"]["digest"] = canonical_digest(tool)
    tool_node["parameter_values"] = {
        "ghost": 1,
        "label": 3,
        "threads": True,
        "threshold": False,
        "labels": ["ok", 2],
        "reference": "/tmp/reference.fa",
        "optional_note": None,
    }

    validation, _ = validate_workflow_graph(graph, [tool])

    diagnostics = validation["diagnostics"]
    assert validation["status"] == "invalid"
    assert {item["code"] for item in diagnostics} == {"WG009", "WG010"}
    assert {item["location"]["port"] for item in diagnostics} == {
        "ghost",
        "label",
        "threads",
        "threshold",
        "labels",
        "reference",
        "optional_note",
    }


def test_workflow_output_requires_exactly_one_inbound_edge():
    graph, tool = fixture()
    graph["edges"] = [
        edge
        for edge in graph["edges"]
        if edge["target"]["node_id"] != "output_clean_reads_1"
    ]

    validation, artifacts = compile_workflow(graph, [tool])

    assert validation["status"] == "invalid"
    output_diagnostics = [
        item
        for item in validation["diagnostics"]
        if item["code"] == "WG015"
    ]
    assert len(output_diagnostics) == 1
    assert output_diagnostics[0]["location"] == {
        "node_id": "output_clean_reads_1",
        "port": "value",
    }
    assert artifacts == []

    graph, tool = fixture()
    inbound = next(
        edge
        for edge in graph["edges"]
        if edge["target"]["node_id"] == "output_clean_reads_1"
    )
    duplicate = deepcopy(inbound)
    duplicate["id"] = f"{inbound['id']}_duplicate"
    graph["edges"].append(duplicate)

    validation, artifacts = compile_workflow(graph, [tool])

    assert validation["status"] == "invalid"
    assert any(
        item["code"] == "WG015"
        and item["location"] == {
            "node_id": "output_clean_reads_1",
            "port": "value",
        }
        for item in validation["diagnostics"]
    )
    assert artifacts == []


def publish_workflow(client, slug: str, payload: dict | None = None):
    document = client.get(f"/api/v1/editor/workflows/{slug}").data
    return client.post(
        f"/api/v1/editor/workflows/{slug}/versions",
        {
            **(payload or {}),
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )


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
    assert compiled.data["wdl_revision"] is None

    history = client.get("/api/v1/editor/workflows/fastp_demo/compilations")
    assert history.status_code == 200
    assert history.data["results"][0]["status"] == "succeeded"
    assert WDLRevision.objects.count() == 0


@pytest.mark.django_db
def test_user_can_create_and_discover_owned_subworkflow_draft():
    user = get_user_model().objects.create_user(username="workflow_author")
    client = APIClient()
    client.force_authenticate(user)

    created = client.post(
        "/api/v1/editor/workflows",
        {
            "slug": "my_qc_subflow",
            "name": "我的 QC 子流程",
            "description": "画布优先创建的可复用子流程",
            "kind": "subworkflow",
        },
        format="json",
    )

    assert created.status_code == 201
    assert created.data["created_by"] == "workflow_author"
    assert created.data["is_mine"] is True
    assert created.data["kind"] == "subworkflow"
    assert created.data["workflow_graph"]["id"] == "my_qc_subflow"
    assert created.data["workflow_graph"]["nodes"][0]["id"] == "input_file"

    listing = client.get("/api/v1/editor/workflows")
    owned = next(item for item in listing.data["results"] if item["slug"] == "my_qc_subflow")
    assert owned["is_mine"] is True
    assert owned["created_by"] == "workflow_author"

    duplicate = client.post(
        "/api/v1/editor/workflows",
        {"slug": "my_qc_subflow", "name": "重复", "kind": "workflow"},
        format="json",
    )
    assert duplicate.status_code == 409
    assert duplicate.data["error"]["code"] == "WORKFLOW_ALREADY_EXISTS"


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

    unprotected = client.post(
        "/api/v1/editor/workflows/fastp_demo/versions", {}, format="json"
    )
    assert unprotected.status_code == 428
    assert unprotected.data["error"]["code"] == "WORKFLOW_PUBLISH_PRECONDITION_REQUIRED"

    first = publish_workflow(client, "fastp_demo")
    second = publish_workflow(client, "fastp_demo")
    assert first.status_code == 201
    assert second.status_code == 201
    assert [first.data["version"], second.data["version"]] == [1, 2]

    versions = client.get("/api/v1/editor/workflows/fastp_demo/versions")
    assert versions.status_code == 200
    assert [item["version"] for item in versions.data["results"]] == [2, 1]

    detail = client.get("/api/v1/editor/workflows/fastp_demo/versions/1")
    assert detail.status_code == 200
    assert detail.data["workflow_graph"] == graph
    assert detail.data["compiled_bundle"]["entrypoint"] == "workflow.wdl"
    assert "version 1.0" in detail.data["compiled_bundle"]["files"]["workflow.wdl"]
    assert detail.data["compiled_digest"]
    assert detail.data["compiler_profile"] == "compiler-core-v1"

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
    assert compiled.data["wdl_revision"]["artifact_role"] == "compiled_snapshot"
    assert compiled.data["wdl_revision"]["run_source"]["version"] == 1
    assert compiled.data["wdl_revision"]["executable"] is False
    assert compiled.data["wdl_revision"]["created_by"] == "local-user"
    assert compiled.data["wdl_revision"]["note"] == "由 WorkflowVersion v1 编译生成。"
    history = client.get("/api/v1/editor/workflows/fastp_demo/compilations")
    assert history.data["results"][0]["workflow_version"] == 1

    mismatched_graph = deepcopy(graph)
    mismatched_graph["name"] = "Not the immutable snapshot"
    mismatched = client.post(
        "/api/v1/compilations",
        {
            "workflow_graph": mismatched_graph,
            "tool_specs": [tool],
            "workflow_version": 1,
        },
        format="json",
    )
    assert mismatched.status_code == 409
    assert mismatched.data["error"]["code"] == "WORKFLOW_VERSION_INPUT_MISMATCH"

    missing_version = client.post(
        "/api/v1/compilations",
        {
            "workflow_graph": graph,
            "tool_specs": [tool],
            "workflow_version": 999,
        },
        format="json",
    )
    assert missing_version.status_code == 404
    assert missing_version.data["error"]["code"] == "WORKFLOW_VERSION_NOT_FOUND"
    assert WDLRevision.objects.count() == 1

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

    first = publish_workflow(client, "fastp_demo")
    assert first.status_code == 201
    assert first.data["reused"] is False

    layout_only_graph = deepcopy(graph)
    layout_only_graph["layout"]["nodes"]["fastp_1"] = {"x": 640, "y": 320}
    layout_saved = client.put(
        "/api/v1/editor/workflows/fastp_demo",
        {
            **document,
            "base_document_version": saved.data["document_version"],
            "base_document_digest": saved.data["document_digest"],
            "workflow_graph": layout_only_graph,
            "editor_document": {
                "nodes": [{"id": "fastp_1", "position": {"x": 640, "y": 320}}]
            },
        },
        format="json",
    )
    assert layout_saved.status_code == 200

    stale_publish = client.post(
        "/api/v1/editor/workflows/fastp_demo/versions",
        {
            "reuse_unchanged": True,
            "base_document_version": saved.data["document_version"],
            "base_document_digest": saved.data["document_digest"],
        },
        format="json",
    )
    assert stale_publish.status_code == 409
    assert stale_publish.data["error"]["code"] == "WORKFLOW_PUBLISH_CONFLICT"

    reused = publish_workflow(client, "fastp_demo", {"reuse_unchanged": True})
    assert reused.status_code == 200
    assert reused.data["version"] == 1
    assert reused.data["reused"] is True
    assert WorkflowVersion.objects.count() == 1

    compiled_reused = client.post(
        "/api/v1/compilations",
        {
            "workflow_graph": layout_only_graph,
            "tool_specs": [tool],
            "workflow_version": 1,
        },
        format="json",
    )
    assert compiled_reused.status_code == 201
    assert compiled_reused.data["wdl_revision"]["run_source"]["version"] == 1

    metadata_saved = client.put(
        "/api/v1/editor/workflows/fastp_demo",
        {
            **document,
            "base_document_version": layout_saved.data["document_version"],
            "base_document_digest": layout_saved.data["document_digest"],
            "description": "Changed workflow metadata.",
        },
        format="json",
    )
    assert metadata_saved.status_code == 200
    changed = publish_workflow(client, "fastp_demo", {"reuse_unchanged": True})
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
    published = publish_workflow(client, "fastp_demo")
    assert published.status_code == 201
    content = (FIXTURE / "expected" / "workflow.wdl").read_text(encoding="utf-8")
    created = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {"content": content, "source": "manual", "workflow_version": 1},
        format="json",
    )
    assert created.status_code == 201
    assert created.data["source"] == "manual"
    assert created.data["artifact_role"] == "derived_draft"
    assert created.data["executable"] is False
    assert created.data["run_source"] is None
    assert created.data["base_workflow_version"]["version"] == 1
    assert created.data["version"] == 1
    assert created.data["content"] == content
    assert created.data["created_by"] == "local-user"
    assert created.data["base_wdl_revision"] is None

    derived_content = content + "\n# derived from WDL v1\n"
    missing_digest = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {
            "content": derived_content,
            "source": "manual",
            "base_wdl_version": 1,
        },
        format="json",
    )
    assert missing_digest.status_code == 428
    assert missing_digest.data["error"]["code"] == "WDL_PRECONDITION_REQUIRED"
    wrong_digest = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {
            "content": derived_content,
            "source": "manual",
            "base_wdl_version": 1,
            "base_wdl_digest": "sha256:wrong",
        },
        format="json",
    )
    assert wrong_digest.status_code == 409
    assert wrong_digest.data["error"]["code"] == "WDL_BASE_REVISION_CONFLICT"
    derived = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {
            "content": derived_content,
            "source": "manual",
            "base_wdl_version": 1,
            "base_wdl_digest": created.data["digest"],
            "note": "Compare a parameter alternative.",
        },
        format="json",
    )
    assert derived.status_code == 201
    assert derived.data["version"] == 2
    assert derived.data["workflow_version"] == 1
    assert derived.data["base_wdl_revision"] == 1
    assert derived.data["note"] == "Compare a parameter alternative."

    second_published = publish_workflow(client, "fastp_demo")
    assert second_published.status_code == 201
    mismatch = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {
            "content": derived_content,
            "source": "manual",
            "base_wdl_version": 1,
            "base_wdl_digest": created.data["digest"],
            "workflow_version": 2,
        },
        format="json",
    )
    assert mismatch.status_code == 409
    assert mismatch.data["error"]["code"] == "WDL_BASE_VERSION_MISMATCH"

    rejected = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {"content": "this is not WDL", "source": "manual"},
        format="json",
    )
    assert rejected.status_code == 422
    assert WDLRevision.objects.count() == 2

    listing = client.get(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions"
    )
    assert listing.data["results"][0]["version"] == 2
    detail = client.get(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions/1"
    )
    assert detail.data["content"] == content

    revision = WDLRevision.objects.get(version=2)
    revision.content = "mutated"
    with pytest.raises(ValidationError):
        revision.save()


@pytest.mark.django_db
def test_manual_wdl_base_without_workflow_version_cannot_be_relabelled():
    graph, tool = fixture()
    client = APIClient()
    client.put(
        "/api/v1/editor/workflows/fastp_demo",
        {
            "name": graph["name"],
            "workflow_graph": graph,
            "tool_specs": [tool],
            "editor_document": {},
        },
        format="json",
    )
    content = (FIXTURE / "expected" / "workflow.wdl").read_text(encoding="utf-8")
    base = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {"content": content, "source": "manual"},
        format="json",
    )
    assert base.status_code == 201
    assert base.data["workflow_version"] is None
    published = publish_workflow(client, "fastp_demo")
    assert published.status_code == 201

    relabelled = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {
            "content": content + "\n# relabelled\n",
            "source": "manual",
            "base_wdl_version": 1,
            "base_wdl_digest": base.data["digest"],
            "workflow_version": 1,
        },
        format="json",
    )
    assert relabelled.status_code == 409
    assert relabelled.data["error"]["code"] == "WDL_BASE_VERSION_MISMATCH"

    inherited = client.post(
        "/api/v1/editor/workflows/fastp_demo/wdl-versions",
        {
            "content": content + "\n# inherited unbound source\n",
            "source": "manual",
            "base_wdl_version": 1,
            "base_wdl_digest": base.data["digest"],
        },
        format="json",
    )
    assert inherited.status_code == 201
    assert inherited.data["workflow_version"] is None
    assert inherited.data["base_wdl_revision"] == 1


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
    published = publish_workflow(client, "read_cleanup")
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
    parent_version = publish_workflow(client, "parent")
    assert parent_version.status_code == 201
    assert parent_version.data["subworkflow_references"] == [reference]

    invalid = client.put(
        "/api/v1/editor/workflows/parent",
        {
            "name": "Parent workflow",
            "base_document_version": parent.data["document_version"],
            "base_document_digest": parent.data["document_digest"],
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
def test_workflow_document_rejects_stale_or_unprotected_writes(django_user_model):
    graph, tool = fixture()
    alice = django_user_model.objects.create_user(username="alice")
    bob = django_user_model.objects.create_user(username="bob")
    alice_client = APIClient()
    alice_client.force_authenticate(alice)
    bob_client = APIClient()
    bob_client.force_authenticate(bob)
    workflow_url = "/api/v1/editor/workflows/collaborative_demo"
    document = {
        "name": "Collaborative demo",
        "description": "Initial draft.",
        "workflow_graph": graph,
        "tool_specs": [tool],
        "editor_document": {"nodes": []},
    }

    created = alice_client.put(workflow_url, document, format="json")
    assert created.status_code == 200

    legacy_saved = bob_client.put(
        workflow_url,
        {**document, "description": "Legacy client save."},
        format="json",
    )
    assert legacy_saved.status_code == 200
    assert legacy_saved.headers["Deprecation"] == "true"
    assert "deprecated" in legacy_saved.headers["Warning"]

    alice_base = alice_client.get(workflow_url).data
    bob_base = bob_client.get(workflow_url).data

    unprotected = bob_client.put(
        workflow_url,
        document,
        format="json",
        HTTP_X_WORKFLOW_CONCURRENCY="required",
    )
    assert unprotected.status_code == 428
    assert unprotected.data["error"]["code"] == "WORKFLOW_PRECONDITION_REQUIRED"

    saved = alice_client.put(
        workflow_url,
        {
            **document,
            "description": "Alice saved this draft.",
            "base_document_version": alice_base["document_version"],
            "base_document_digest": alice_base["document_digest"],
        },
        format="json",
    )
    assert saved.status_code == 200
    assert saved.data["document_version"] == alice_base["document_version"] + 1
    assert saved.data["updated_by"] == "alice"

    unchanged = bob_client.put(
        workflow_url,
        {
            **saved.data,
            "base_document_version": saved.data["document_version"],
            "base_document_digest": saved.data["document_digest"],
        },
        format="json",
    )
    assert unchanged.status_code == 200
    assert unchanged.data["document_version"] == saved.data["document_version"]
    assert unchanged.data["document_digest"] == saved.data["document_digest"]
    assert unchanged.data["updated_by"] == "alice"

    stale = bob_client.put(
        workflow_url,
        {
            **document,
            "description": "Bob's stale draft.",
            "base_document_version": bob_base["document_version"],
            "base_document_digest": bob_base["document_digest"],
        },
        format="json",
    )
    assert stale.status_code == 409
    assert stale.data["error"]["code"] == "WORKFLOW_DOCUMENT_CONFLICT"
    current = stale.data["error"]["details"]["current"]
    assert current["description"] == "Alice saved this draft."
    assert current["updated_by"] == "alice"
    assert WorkflowDocument.objects.get(slug="collaborative_demo").description == (
        "Alice saved this draft."
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
    published = publish_workflow(client, "read_cleanup")
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

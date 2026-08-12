import json
from copy import deepcopy
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from workflows.models import (
    ToolDocument,
    ToolVersion,
    WDLGraphProposal,
    WorkflowDocument,
)


ROOT = Path(__file__).resolve().parents[2]
FASTP_FIXTURE = ROOT / "examples" / "phase1-fastp"
CHAIN_FIXTURE = ROOT / "examples" / "phase1-fastp-bwa"


def _fixture():
    graph = json.loads(
        (CHAIN_FIXTURE / "workflow-graph.json").read_text(encoding="utf-8")
    )
    tools = [
        json.loads((FASTP_FIXTURE / "tool-fastp.json").read_text(encoding="utf-8")),
        json.loads((CHAIN_FIXTURE / "tool-bwa-mem.json").read_text(encoding="utf-8")),
    ]
    wdl = (CHAIN_FIXTURE / "expected" / "workflow.wdl").read_text(encoding="utf-8")
    return graph, tools, wdl


def _create_document_and_revision(
    client: APIClient,
    *,
    content: str | None = None,
    graph_override: dict | None = None,
    tools_override: list[dict] | None = None,
):
    graph, tools, wdl = _fixture()
    graph = deepcopy(graph_override or graph)
    tools = deepcopy(tools_override or tools)
    saved = client.put(
        "/api/v1/editor/workflows/fastp_bwa_demo",
        {
            "name": graph["name"],
            "description": graph["description"],
            "workflow_graph": graph,
            "tool_specs": tools,
            "editor_document": {
                "nodes": [
                    {"id": node_id, "position": position}
                    for node_id, position in graph["layout"]["nodes"].items()
                ],
                "viewport": graph["layout"]["viewport"],
            },
        },
        format="json",
    )
    assert saved.status_code == 200
    revision = client.post(
        "/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions",
        {"content": content or wdl, "source": "manual"},
        format="json",
    )
    assert revision.status_code == 201
    return saved.data, revision.data


@pytest.mark.django_db
def test_unchanged_wdl_round_trip_creates_reviewable_noop_proposal():
    client = APIClient()
    document, revision = _create_document_and_revision(client)

    missing_precondition = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {},
        format="json",
    )
    assert missing_precondition.status_code == 428

    proposed = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )

    assert proposed.status_code == 201
    assert proposed.data["status"] == "ready", proposed.data["blocking_issues"]
    assert proposed.data["summary"] == {
        "workflow_change_count": 0,
        "tool_draft_count": 0,
        "instance_change_count": 0,
    }
    assert proposed.data["required_confirmations"] == []
    assert ToolDocument.objects.count() == 0
    assert WorkflowDocument.objects.get(slug="fastp_bwa_demo").document_version == 1


@pytest.mark.django_db
def test_call_parameter_change_updates_node_config_without_new_tool_version():
    client = APIClient()
    _, _, wdl = _fixture()
    changed_wdl = wdl.replace(
        "      threads = 8\n  }\n\n  output",
        "      threads = 12\n  }\n\n  output",
    )
    document, revision = _create_document_and_revision(client, content=changed_wdl)

    proposed = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )

    assert proposed.status_code == 201
    assert proposed.data["summary"] == {
        "workflow_change_count": 0,
        "tool_draft_count": 0,
        "instance_change_count": 1,
    }
    assert proposed.data["changes"]["instance_parameters"] == [
        {
            "kind": "parameter_changed",
            "subject": "bwa_mem_1.threads",
            "detail": "8 → 12",
        }
    ]
    assert proposed.data["required_confirmations"] == ["instance_parameters"]
    assert proposed.data["tool_drafts"] == []

    applied = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        {
            "proposal_digest": proposed.data["proposal_digest"],
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
            "confirm_sections": ["instance_parameters"],
        },
        format="json",
    )

    assert applied.status_code == 200
    assert ToolDocument.objects.count() == 0
    node = next(
        item
        for item in WorkflowDocument.objects.get(slug="fastp_bwa_demo").workflow_graph[
            "nodes"
        ]
        if item["id"] == "bwa_mem_1"
    )
    assert node["parameter_values"]["threads"] == 12


@pytest.mark.django_db
def test_omitted_call_parameter_and_unused_tool_spec_are_preserved():
    client = APIClient()
    graph, tools, wdl = _fixture()
    next(node for node in graph["nodes"] if node["id"] == "bwa_mem_1")[
        "parameter_values"
    ]["threads"] = 12
    unused = deepcopy(tools[0])
    unused.update(
        {
            "id": "unused_qc",
            "name": "unused_qc",
            "display_name": "Unused QC",
            "tool_version": "1.0.0",
        }
    )
    changed_wdl = wdl.replace(
        "      reference = input_reference,\n      threads = 8",
        "      reference = input_reference",
    ).replace(
        "      threads = 4\n  }\n\n  call bwa_mem",
        "      threads = 5\n  }\n\n  call bwa_mem",
    )
    document, revision = _create_document_and_revision(
        client,
        content=changed_wdl,
        graph_override=graph,
        tools_override=[*tools, unused],
    )

    proposed = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )

    assert proposed.status_code == 201, proposed.data
    assert proposed.data["changes"]["instance_parameters"] == [
        {
            "kind": "parameter_changed",
            "subject": "fastp_1.threads",
            "detail": "4 → 5",
        }
    ]
    applied = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        {
            "proposal_digest": proposed.data["proposal_digest"],
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
            "confirm_sections": proposed.data["required_confirmations"],
        },
        format="json",
    )

    assert applied.status_code == 200, applied.data
    updated = WorkflowDocument.objects.get(slug="fastp_bwa_demo")
    bwa_node = next(
        node for node in updated.workflow_graph["nodes"] if node["id"] == "bwa_mem_1"
    )
    assert bwa_node["parameter_values"]["threads"] == 12
    assert {spec["id"] for spec in updated.tool_specs} == {
        "fastp",
        "bwa_mem",
        "unused_qc",
    }


@pytest.mark.django_db
def test_changed_task_creates_new_tool_draft_only_after_confirmed_apply():
    client = APIClient()
    graph, tools, wdl = _fixture()
    changed_wdl = wdl.replace(
        "bwa mem -t ~{threads}",
        "bwa mem -K 100000000 -t ~{threads}",
    )
    document, revision = _create_document_and_revision(client, content=changed_wdl)
    for tool in tools:
        ToolVersion.objects.create(
            tool_id=tool["id"],
            version=tool["tool_version"],
            name=tool["name"],
            digest=next(
                node["tool_ref"]["digest"]
                for node in graph["nodes"]
                if node.get("tool_ref", {}).get("id") == tool["id"]
            ),
            tool_spec=tool,
        )

    proposed = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )

    assert proposed.status_code == 201
    assert proposed.data["status"] == "ready", proposed.data["blocking_issues"]
    assert proposed.data["summary"]["tool_draft_count"] == 1
    assert proposed.data["changes"]["tool_versions"] == [
        {
            "kind": "tool_draft_created",
            "subject": "bwa_mem",
            "detail": "0.7.17 → 0.7.17-wdl.1；固定内容：命令模板",
        }
    ]
    assert proposed.data["tool_drafts"] == [
        {
            "tool_id": "bwa_mem",
            "base_version": "0.7.17",
            "proposed_version": "0.7.17-wdl.1",
            "changed_fields": ["命令模板"],
            "field_diffs": [
                {
                    "field": "command",
                    "label": "命令模板",
                    "before": tools[1]["command"],
                    "after": {
                        **tools[1]["command"],
                        "template": tools[1]["command"]["template"].replace(
                            "bwa mem -t {{ inputs.threads }}",
                            "bwa mem -K 100000000 -t {{ inputs.threads }}",
                        ),
                    },
                }
            ],
        }
    ]
    assert ToolDocument.objects.count() == 0
    assert ToolVersion.objects.count() == 2

    incomplete = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        {
            "proposal_digest": proposed.data["proposal_digest"],
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
            "confirm_sections": [],
        },
        format="json",
    )
    assert incomplete.status_code == 400
    assert ToolDocument.objects.count() == 0

    applied = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        {
            "proposal_digest": proposed.data["proposal_digest"],
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
            "confirm_sections": proposed.data["required_confirmations"],
        },
        format="json",
    )

    assert applied.status_code == 200
    draft = ToolDocument.objects.get(tool_id="bwa_mem")
    assert draft.draft_spec["tool_version"] == "0.7.17-wdl.1"
    assert "bwa mem -K 100000000" in draft.draft_spec["command"]["template"]
    assert ToolVersion.objects.count() == 2
    updated = WorkflowDocument.objects.get(slug="fastp_bwa_demo")
    assert updated.document_version == 2
    assert (
        next(
            node["tool_ref"]["tool_version"]
            for node in updated.workflow_graph["nodes"]
            if node["id"] == "bwa_mem_1"
        )
        == "0.7.17-wdl.1"
    )
    blocked_publish = client.post(
        "/api/v1/editor/workflows/fastp_bwa_demo/versions",
        {
            "base_document_version": updated.document_version,
            "base_document_digest": client.get(
                "/api/v1/editor/workflows/fastp_bwa_demo"
            ).data["document_digest"],
        },
        format="json",
    )
    assert blocked_publish.status_code == 422
    assert blocked_publish.data["error"]["code"] == "WORKFLOW_TOOL_VERSION_UNPUBLISHED"
    assert blocked_publish.data["error"]["details"][0]["tool_id"] == "bwa_mem"

    draft_payload = client.get("/api/v1/tools/bwa_mem/drafts").data
    published_tool = client.post(
        "/api/v1/tools/bwa_mem/publish",
        {
            "base_draft_version": draft_payload["draft_version"],
            "base_draft_digest": draft_payload["draft_digest"],
        },
        format="json",
    )
    assert published_tool.status_code == 201
    published_workflow = client.post(
        "/api/v1/editor/workflows/fastp_bwa_demo/versions",
        {
            "base_document_version": updated.document_version,
            "base_document_digest": client.get(
                "/api/v1/editor/workflows/fastp_bwa_demo"
            ).data["document_digest"],
        },
        format="json",
    )
    assert published_workflow.status_code == 201


@pytest.mark.django_db
def test_changed_canvas_or_dynamic_wdl_never_overwrites_current_draft():
    client = APIClient()
    graph, tools, wdl = _fixture()
    changed_wdl = wdl.replace("bwa mem -t", "bwa mem -K 100000000 -t")
    document, revision = _create_document_and_revision(client, content=changed_wdl)
    proposed = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )
    assert proposed.status_code == 201

    changed_graph = deepcopy(graph)
    changed_graph["description"] = "concurrent canvas edit"
    concurrent = client.put(
        "/api/v1/editor/workflows/fastp_bwa_demo",
        {
            "name": graph["name"],
            "description": graph["description"],
            "workflow_graph": changed_graph,
            "tool_specs": tools,
            "editor_document": {},
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
        HTTP_X_WORKFLOW_CONCURRENCY="required",
    )
    assert concurrent.status_code == 200
    conflict = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        {
            "proposal_digest": proposed.data["proposal_digest"],
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
            "confirm_sections": proposed.data["required_confirmations"],
        },
        format="json",
    )
    assert conflict.status_code == 409
    assert ToolDocument.objects.count() == 0
    assert WDLGraphProposal.objects.get(pk=proposed.data["id"]).status == "ready"

    scatter_call = """  scatter (index in [1]) {
    call fastp as ignored_fastp {
      input:
        reads_1 = input_reads_1,
        reads_2 = input_reads_2,
        threads = index
    }
  }

"""
    scatter_wdl = wdl.replace(
        "  call fastp as fastp_1 {", scatter_call + "  call fastp as fastp_1 {"
    )
    blocked_revision = client.post(
        "/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions",
        {"content": scatter_wdl, "source": "manual"},
        format="json",
    )
    assert blocked_revision.status_code == 201
    blocked = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{blocked_revision.data['version']}/graph-proposals",
        {
            "base_document_version": concurrent.data["document_version"],
            "base_document_digest": concurrent.data["document_digest"],
        },
        format="json",
    )
    assert blocked.status_code == 201
    assert blocked.data["status"] == "blocked"
    assert "Scatter" in blocked.data["blocking_issues"][0]


@pytest.mark.django_db
def test_tool_draft_conflict_rolls_back_the_whole_proposal():
    client = APIClient()
    _, _, wdl = _fixture()
    changed_wdl = wdl.replace("bwa mem -t", "bwa mem -K 100000000 -t").replace(
        "fastp \\",
        "fastp --dont-overwrite-existing-draft \\",
    )
    document, revision = _create_document_and_revision(client, content=changed_wdl)
    proposed = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )
    assert proposed.status_code == 201
    assert proposed.data["summary"]["tool_draft_count"] == 2
    ToolDocument.objects.create(
        tool_id="fastp",
        draft_spec={"id": "fastp", "tool_version": "other-draft"},
    )

    conflict = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        {
            "proposal_digest": proposed.data["proposal_digest"],
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
            "confirm_sections": proposed.data["required_confirmations"],
        },
        format="json",
    )

    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "TOOL_DRAFT_CONFLICT"
    assert not ToolDocument.objects.filter(tool_id="bwa_mem").exists()
    assert WorkflowDocument.objects.get(slug="fastp_bwa_demo").document_version == 1


@pytest.mark.django_db
def test_unsupported_runtime_and_invalid_confirmations_are_rejected_safely():
    client = APIClient()
    _, _, wdl = _fixture()
    runtime_wdl = wdl.replace(
        '    memory: "16 GB"',
        '    memory: "16 GB"\n    maxRetries: 2',
        1,
    )
    document, revision = _create_document_and_revision(client, content=runtime_wdl)
    blocked = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )
    assert blocked.status_code == 201
    assert blocked.data["status"] == "blocked"
    assert "maxRetries" in blocked.data["blocking_issues"][0]

    _, safe_revision = _create_document_and_revision(client, content=wdl)
    proposed = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{safe_revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )
    invalid = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        {
            "proposal_digest": proposed.data["proposal_digest"],
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
            "confirm_sections": [{}],
        },
        format="json",
    )
    assert invalid.status_code == 400


@pytest.mark.django_db
def test_applied_proposal_only_accepts_identical_idempotent_request():
    client = APIClient()
    document, revision = _create_document_and_revision(client)
    proposed = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/{revision['version']}/graph-proposals",
        {
            "base_document_version": document["document_version"],
            "base_document_digest": document["document_digest"],
        },
        format="json",
    )
    body = {
        "proposal_digest": proposed.data["proposal_digest"],
        "base_document_version": document["document_version"],
        "base_document_digest": document["document_digest"],
        "confirm_sections": proposed.data["required_confirmations"],
    }
    applied = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        body,
        format="json",
    )
    assert applied.status_code == 200

    repeated = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        body,
        format="json",
    )
    assert repeated.status_code == 200
    mismatched = client.post(
        f"/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/{proposed.data['id']}/apply",
        {**body, "proposal_digest": "sha256:wrong"},
        format="json",
    )
    assert mismatched.status_code == 409
    assert mismatched.data["error"]["code"] == "WDL_GRAPH_PROPOSAL_CONFLICT"

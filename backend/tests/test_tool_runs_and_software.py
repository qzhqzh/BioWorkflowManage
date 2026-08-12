from __future__ import annotations

from pathlib import Path

import pytest
from rest_framework.test import APIClient

from compiler_core import canonical_digest
from workflows.models import (
    AnalysisRun,
    SoftwareAsset,
    SoftwareAuditEvent,
    ToolVersion,
)


def shell_tool_spec(*, with_file: bool = False) -> dict:
    inputs = [
        {
            "name": "message",
            "label": "内容",
            "wdl_type": "String",
            "semantic_type": "core.value.string",
            "required": True,
        }
    ]
    command = 'mkdir -p outputs\nprintf "%s\\n" "{{ inputs.message }}" > outputs/result.txt'
    if with_file:
        inputs.append(
            {
                "name": "source",
                "label": "输入文件",
                "wdl_type": "File",
                "semantic_type": "core.file.any",
                "required": True,
            }
        )
        command = "mkdir -p outputs\ncat {{ inputs.source }} > outputs/result.txt"
    return {
        "schema_version": "1.0.0",
        "id": "shell_preview",
        "name": "shell",
        "display_name": "Shell preview",
        "tool_version": "1.0.0",
        "description": "Small standalone task fixture.",
        "container": {"engine": "docker", "image": "ubuntu:24.04"},
        "inputs": inputs,
        "outputs": [
            {
                "name": "result",
                "label": "结果",
                "wdl_type": "File",
                "semantic_type": "core.file.any",
                "capture": {"mode": "path", "value": "outputs/result.txt"},
            }
        ],
        "command": {"shell": "bash", "strict_mode": True, "template": command},
        "runtime": {"cpu": 1, "memory_gb": 1, "disk_gb": 1},
    }


def create_tool_version(*, with_file: bool = False) -> ToolVersion:
    spec = shell_tool_spec(with_file=with_file)
    return ToolVersion.objects.create(
        tool_id=spec["id"],
        version=spec["tool_version"],
        name=spec["display_name"],
        digest=canonical_digest(spec),
        tool_spec=spec,
    )


@pytest.mark.django_db
def test_software_library_tracks_versions_links_and_audit():
    client = APIClient()
    tool = create_tool_version()

    created = client.post(
        "/api/v1/software",
        {
            "slug": "samtools",
            "name": "samtools",
            "summary": "SAM/BAM/CRAM utilities",
            "tags": ["alignment", "BAM", "bam"],
            "notes": "Keep release-specific compatibility notes here.",
        },
        format="json",
    )
    assert created.status_code == 201
    assert created.data["tags"] == ["alignment", "BAM"]
    assert created.data["metadata_version"] == 1

    missing_precondition = client.patch(
        "/api/v1/software/samtools",
        {"name": "Samtools"},
        format="json",
    )
    assert missing_precondition.status_code == 428

    updated = client.patch(
        "/api/v1/software/samtools",
        {
            "name": "Samtools",
            "summary": "Updated collaboratively",
            "base_metadata_version": created.data["metadata_version"],
        },
        format="json",
    )
    assert updated.status_code == 200
    assert updated.data["metadata_version"] == 2

    partial_update = client.patch(
        "/api/v1/software/samtools",
        {
            "notes": "Only this field changed.",
            "base_metadata_version": updated.data["metadata_version"],
        },
        format="json",
    )
    assert partial_update.status_code == 200
    assert partial_update.data["name"] == "Samtools"
    assert partial_update.data["metadata_version"] == 3

    release = client.post(
        "/api/v1/software/samtools/releases",
        {
            "version": "1.20",
            "description": "Pinned production release",
            "container_images": ["quay.io/biocontainers/samtools:1.20"],
        },
        format="json",
    )
    assert release.status_code == 201

    link = client.post(
        "/api/v1/software/samtools/tool-links",
        {
            "tool_id": tool.tool_id,
            "tool_version": tool.version,
            "software_version": "1.20",
            "role": "dependency",
        },
        format="json",
    )
    assert link.status_code == 201
    assert link.data["tool"]["id"] == tool.tool_id

    detail = client.get("/api/v1/software/samtools")
    assert detail.status_code == 200
    assert detail.data["release_count"] == 1
    assert detail.data["tool_count"] == 1
    assert detail.data["tool_links"][0]["release"]["version"] == "1.20"
    assert SoftwareAuditEvent.objects.filter(
        software__slug="samtools", action="tool_link"
    ).exists()


@pytest.mark.django_db
def test_tool_version_can_be_queued_as_standalone_test():
    client = APIClient()
    tool = create_tool_version()

    response = client.post(
        "/api/v1/tool-test-runs",
        {
            "tool_id": tool.tool_id,
            "tool_version": tool.version,
            "label": "echo smoke",
            "inputs": {"message": "hello"},
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    run = AnalysisRun.objects.get(pk=response.data["id"])
    assert run.run_kind == AnalysisRun.Kind.TOOL_TEST
    assert run.tool_version == tool
    assert run.input_values == {"tool_test.input_1_message": "hello"}
    assert run.source_bundle["entrypoint"] == "tool-test.wdl"
    assert "workflow tool_test" in run.source_bundle["files"]["tool-test.wdl"]
    assert "call shell_preview as task_under_test" in run.source_bundle["files"]["tool-test.wdl"]
    assert response.data["tool"]["digest"] == tool.digest
    assert response.data["actor"] == "local-user"
    assert run.request_payload["output_labels"] == {
        "tool_test.output_1_result": "结果"
    }

    listed = client.get(
        f"/api/v1/tool-test-runs?tool_id={tool.tool_id}&version={tool.version}"
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.data["results"]] == [str(run.id)]

    analysis_list = client.get("/api/v1/analysis-runs")
    assert analysis_list.status_code == 200
    assert analysis_list.data["results"] == []


@pytest.mark.django_db
def test_tool_test_file_input_is_scoped_and_manifested(settings, tmp_path: Path):
    rawdata = tmp_path / "rawdata"
    database = tmp_path / "databases"
    execution_rawdata = Path("/analysis/rawdata")
    execution_database = Path("/analysis/databases")
    rawdata.mkdir()
    database.mkdir()
    (rawdata / "small.txt").write_text("small fixture\n", encoding="utf-8")
    settings.ANALYSIS_RAWDATA_ROOT = rawdata
    settings.ANALYSIS_RAWDATA_EXECUTION_ROOT = execution_rawdata
    settings.ANALYSIS_DATABASE_ROOT = database
    settings.ANALYSIS_DATABASE_EXECUTION_ROOT = execution_database
    tool = create_tool_version(with_file=True)
    client = APIClient()

    response = client.post(
        "/api/v1/tool-test-runs",
        {
            "tool_id": tool.tool_id,
            "tool_version": tool.version,
            "inputs": {
                "message": "unused",
                "source": {"source": "rawdata", "path": "small.txt"},
            },
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    run = AnalysisRun.objects.get(pk=response.data["id"])
    assert run.input_values["tool_test.input_2_source"] == "/analysis/rawdata/small.txt"
    manifest = run.request_payload["input_resource_manifest"]
    assert manifest["files"][0]["relative_path"] == "small.txt"
    assert manifest["files"][0]["verification"] == "identity"

    rejected = client.post(
        "/api/v1/tool-test-runs",
        {
            "tool_id": tool.tool_id,
            "tool_version": tool.version,
            "inputs": {
                "message": "unused",
                "source": {"source": "rawdata", "path": "../outside.txt"},
            },
        },
        format="json",
    )
    assert rejected.status_code == 400
    assert rejected.data["error"]["code"] == "TOOL_TEST_INPUT_INVALID"


@pytest.mark.django_db
def test_shell_tool_does_not_require_software_link():
    tool = create_tool_version()

    assert tool.software_links.count() == 0
    assert SoftwareAsset.objects.count() == 0

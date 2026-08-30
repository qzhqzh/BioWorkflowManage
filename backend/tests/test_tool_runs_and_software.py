from __future__ import annotations

import os
import time
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
from workflows.analysis_runtime import _verify_run_resource_manifests
from workflows.integration_outputs import ResourceSnapshotBudget, build_output_manifest
from workflows.tool_runs import _managed_resource


pytestmark = pytest.mark.usefixtures("auth_disabled")


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
    assert manifest["files"][0]["verification"] == "identity_v2"
    assert manifest["files"][0]["ctime_ns"] > 0
    assert "sha256" not in manifest["files"][0]

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
def test_tool_managed_directory_input_is_content_manifested_and_verified(
    settings, tmp_path: Path
):
    database = tmp_path / "databases"
    resource = database / "bundle"
    resource.mkdir(parents=True)
    child = resource / "reference.fa"
    child.write_bytes(b">chr1\nACGT\n")
    settings.ANALYSIS_DATABASE_ROOT = database
    settings.ANALYSIS_DATABASE_EXECUTION_ROOT = database
    manifests = {"rawdata": [], "database": []}

    _managed_resource(
        {"source": "database", "path": "bundle"},
        kind="directory",
        input_name="reference",
        manifests=manifests,
        snapshot_budget=ResourceSnapshotBudget(),
    )
    item = manifests["database"][0]
    assert item["verification"] == "directory_identity_sha256"
    assert item["digest"].startswith("sha256:")
    assert item["entry_count"] == 1

    run = type(
        "Run",
        (),
        {
            "request_payload": {
                "database_resource_manifest": {
                    "schema_version": 1,
                    "resources": manifests["database"],
                }
            }
        },
    )()
    child_stat = child.stat()
    time.sleep(0.01)
    child.write_bytes(b">chr1\nTGCA\n")
    os.utime(child, ns=(child_stat.st_atime_ns, child_stat.st_mtime_ns))

    with pytest.raises(RuntimeError, match="目录校验和不匹配"):
        _verify_run_resource_manifests(run)


@pytest.mark.django_db
def test_tool_output_download_rejects_tampered_persisted_manifest(
    settings, tmp_path: Path
):
    runs = tmp_path / "runs"
    run_directory = runs / "tool-manifest"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("original\n", encoding="utf-8")
    settings.ANALYSIS_RUN_ROOT = runs
    settings.ANALYSIS_RUN_EXECUTION_ROOT = runs
    tool = create_tool_version()
    client = APIClient()
    queued = client.post(
        "/api/v1/tool-test-runs",
        {
            "tool_id": tool.tool_id,
            "tool_version": tool.version,
            "inputs": {"message": "hello"},
        },
        format="json",
    )
    assert queued.status_code == 201, queued.data
    run = AnalysisRun.objects.get(pk=queued.data["id"])
    run.status = AnalysisRun.Status.SUCCEEDED
    run.progress = 100
    run.work_directory = str(run_directory)
    run.outputs = {"outputs": {"tool_test.output_1_result": str(output)}}
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert error is None
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(
        update_fields=[
            "status",
            "progress",
            "work_directory",
            "outputs",
            "output_manifest",
            "output_status",
            "updated_at",
        ]
    )

    detail = client.get(f"/api/v1/tool-test-runs/{run.id}")
    assert detail.status_code == 200
    assert detail.data["output_status"] == AnalysisRun.OutputStatus.COMPLETE
    assert detail.data["error_code"] == ""
    item = detail.data["outputs"][0]
    downloaded = client.get(item["download_url"])
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == b"original\n"

    output_stat = output.stat()
    output.write_text("tampered\n", encoding="utf-8")
    os.utime(output, ns=(output_stat.st_atime_ns, output_stat.st_mtime_ns))
    changed = client.get(item["download_url"])
    assert changed.status_code == 409
    assert changed.data["error"]["code"] == "ANALYSIS_OUTPUT_CHANGED"


@pytest.mark.django_db
def test_tool_incomplete_v2_manifest_keeps_verified_file_downloadable(
    settings, tmp_path: Path, monkeypatch
):
    runs = tmp_path / "runs"
    run_directory = runs / "tool-partial"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("verified\n", encoding="utf-8")
    settings.ANALYSIS_RUN_ROOT = runs
    settings.ANALYSIS_RUN_EXECUTION_ROOT = runs
    settings.ANALYSIS_OUTPUT_VALUE_MAX_BYTES = 16
    tool = create_tool_version()
    run = AnalysisRun.objects.create(
        tool_version=tool,
        run_kind=AnalysisRun.Kind.TOOL_TEST,
        workflow_name="tool_test",
        sample_id="tool-partial",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        request_payload={
            "integration_output_contract": [
                {
                    "key": "tool_test.output_1_result",
                    "wdl_type": "File",
                    "required": True,
                },
                {
                    "key": "tool_test.note",
                    "wdl_type": "String",
                    "required": True,
                },
            ],
            "output_labels": {"tool_test.output_1_result": "结果"},
        },
        outputs={
            "outputs": {
                "tool_test.output_1_result": str(output),
                "tool_test.note": "sensitive-marker-" + "x" * 100,
            }
        },
    )
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["output_manifest", "output_status", "updated_at"])
    monkeypatch.setattr(
        "workflows.tool_runs._flatten_outputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v2 manifest must not read legacy outputs")
        ),
    )
    client = APIClient()

    listed = client.get("/api/v1/tool-test-runs")
    detail = client.get(f"/api/v1/tool-test-runs/{run.id}")
    downloaded = client.get(
        f"/api/v1/tool-test-runs/{run.id}/outputs",
        {"key": "tool_test.output_1_result"},
    )
    incomplete = client.get(
        f"/api/v1/tool-test-runs/{run.id}/outputs",
        {"key": "tool_test.note"},
    )

    assert listed.data["view"] == "summary"
    assert listed.data["results"][0]["outputs"] == []
    assert "sensitive-marker" not in str(listed.data)
    detail_outputs = {item["key"]: item for item in detail.data["outputs"]}
    assert detail_outputs["tool_test.output_1_result"]["download_url"]
    assert detail_outputs["tool_test.note"]["kind"] == "unverifiable"
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == b"verified\n"
    assert incomplete.status_code == 409
    assert incomplete.data["error"]["code"] == "ANALYSIS_OUTPUT_INCOMPLETE"


@pytest.mark.django_db
def test_tool_output_download_refuses_legacy_file_without_manifest(
    settings, tmp_path: Path
):
    runs = tmp_path / "runs"
    run_directory = runs / "tool-legacy"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("legacy\n", encoding="utf-8")
    settings.ANALYSIS_RUN_ROOT = runs
    settings.ANALYSIS_RUN_EXECUTION_ROOT = runs
    tool = create_tool_version()
    run = AnalysisRun.objects.create(
        tool_version=tool,
        run_kind=AnalysisRun.Kind.TOOL_TEST,
        workflow_name="tool_test",
        sample_id="tool-legacy",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"tool_test.output_1_result": str(output)}},
        output_manifest={
            "schema_version": 1,
            "items": [
                {
                    "key": "tool_test.output_1_result",
                    "kind": "file",
                    "path": str(output),
                    "sha256": "sha256:" + "0" * 64,
                }
            ],
        },
    )

    detail = APIClient().get(f"/api/v1/tool-test-runs/{run.id}")
    response = APIClient().get(
        f"/api/v1/tool-test-runs/{run.id}/outputs",
        {"key": "tool_test.output_1_result"},
    )

    assert detail.status_code == 200
    assert "download_url" not in detail.data["outputs"][0]
    assert response.status_code == 409
    assert response.data["error"]["code"] == "ANALYSIS_OUTPUT_UNVERIFIED"


@pytest.mark.django_db
def test_shell_tool_does_not_require_software_link():
    tool = create_tool_version()

    assert tool.software_links.count() == 0
    assert SoftwareAsset.objects.count() == 0

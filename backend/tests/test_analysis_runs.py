from __future__ import annotations

import gzip
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from workflows.analysis_runtime import (
    GIB,
    _available_memory_bytes,
    _is_infrastructure_error,
    _result_error,
    claim_next_run,
    execute_analysis_run,
)
from workflows.analysis_runs import _parse_miniwdl_timing
from workflows.models import AnalysisRun, WDLAsset, WDLSourceFile, WDLSourceRevision


pytestmark = pytest.mark.django_db


def test_result_error_prefers_nested_runtime_message(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "error": "RunFailed",
                "cause": {
                    "error": "RuntimeError",
                    "message": "docker image not found: example/image:tag",
                },
            }
        ),
        encoding="utf-8",
    )

    assert _result_error(result_path, "fallback") == (
        "docker image not found: example/image:tag"
    )


def test_available_memory_bytes_reads_memavailable(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 2048 kB\n", encoding="ascii")

    assert _available_memory_bytes(meminfo) == 2 * 1024 * 1024


def test_infrastructure_error_recognizes_interrupted_docker_task(tmp_path):
    log_path = tmp_path / "miniwdl.log"
    log_path.write_text(
        '{"message":"docker task running, exit code = -1","error":"Interrupted"}\n',
        encoding="utf-8",
    )

    assert _is_infrastructure_error("RuntimeError", log_path) is True


def test_infrastructure_error_does_not_retry_application_failure(tmp_path):
    log_path = tmp_path / "miniwdl.log"
    log_path.write_text(
        '{"message":"docker swarm resources","workers":1}\n'
        '{"message":"docker task exit","exit_code":2}\n',
        encoding="utf-8",
    )

    assert _is_infrastructure_error("command failed with exit status 2", log_path) is False


def test_timing_marks_interrupted_task_as_failed(tmp_path):
    log_path = tmp_path / "miniwdl.log"
    events = [
        {"message": "workflow start", "source": "wdl.w:Test", "timestamp": 1.0},
        {
            "message": "task setup",
            "name": "QC",
            "source": "wdl.w:Test.t:call-QC",
            "timestamp": 2.0,
        },
        {
            "message": "task QC failed",
            "error": "Interrupted",
            "source": "wdl.w:Test.t:call-QC",
            "timestamp": 5.0,
        },
    ]
    log_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    stat = log_path.stat()

    timing = _parse_miniwdl_timing(str(log_path), stat.st_mtime_ns, stat.st_size)

    assert timing["tasks"][0]["status"] == "failed"
    assert timing["tasks"][0]["duration_seconds"] == 3.0


def _write_fastq(path, mate: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="ascii") as handle:
        handle.write(f"@read-1/{mate}\nACGT\n+\nIIII\n")


def _asset(slug: str, workflow_name: str):
    source = f"""version 1.0

workflow {workflow_name} {{
  input {{
    String sample
  }}
  output {{
    String sample_out = sample
  }}
}}
"""
    asset = WDLAsset.objects.create(
        slug=slug,
        name=workflow_name,
        source_filename="workflow.wdl",
    )
    revision = WDLSourceRevision.objects.create(
        asset=asset,
        version=1,
        operation=WDLSourceRevision.Operation.IMPORT,
        content=source,
        digest="sha256:test",
        analysis={
            "diagnostics": [],
            "workflows": [{"structure": {"call_count": 1}}],
        },
    )
    WDLSourceFile.objects.create(
        revision=revision,
        path="workflow.wdl",
        content=source,
        digest="sha256:test-file",
        is_entry=True,
    )
    return asset, revision


def test_worker_leaves_run_queued_until_memory_is_available(settings, monkeypatch):
    asset, revision = _asset("resource-gated", "ResourceGated")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="ResourceGated",
        sample_id="SAMPLE01",
    )
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 8
    monkeypatch.setattr(
        "workflows.analysis_runtime._available_memory_bytes",
        lambda: 4 * GIB,
    )

    assert claim_next_run() is None
    assert claim_next_run() is None

    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.QUEUED
    assert run.current_step == "等待计算资源（可用内存 4.0 GB，至少需要 8 GB）"
    assert run.events.filter(kind="resource").count() == 1


def test_execute_run_retries_infrastructure_failure_once(settings, tmp_path, monkeypatch):
    asset, revision = _asset("retryable", "Retryable")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="Retryable",
        sample_id="SAMPLE01",
    )
    settings.ANALYSIS_RUN_ROOT = tmp_path / "runs"
    settings.ANALYSIS_INFRASTRUCTURE_RETRIES = 1
    settings.ANALYSIS_INFRASTRUCTURE_RETRY_DELAY_SECONDS = 0
    calls = 0

    class FakeProcess:
        def __init__(self, arguments, **kwargs):
            nonlocal calls
            del kwargs
            calls += 1
            result_path = arguments[arguments.index("-o") + 1]
            if calls == 1:
                with open(result_path, "w", encoding="utf-8") as handle:
                    json.dump({"error": {"message": "Interrupted"}}, handle)
                self.stderr = io.StringIO(
                    '{"message":"docker task running, exit code = -1",'
                    '"level":"ERROR","error":"Interrupted"}\n'
                )
                self.exit_code = 1
            else:
                with open(result_path, "w", encoding="utf-8") as handle:
                    json.dump({"outputs": {"Retryable.sample": "SAMPLE01"}}, handle)
                self.stderr = io.StringIO(
                    '{"message":"workflow done","level":"NOTICE"}\n'
                )
                self.exit_code = 0

        def wait(self):
            return self.exit_code

    monkeypatch.setattr("workflows.analysis_runtime.shutil.which", lambda _: "/bin/miniwdl")
    monkeypatch.setattr("workflows.analysis_runtime.subprocess.Popen", FakeProcess)

    execute_analysis_run(run)

    run.refresh_from_db()
    assert calls == 2
    assert run.status == AnalysisRun.Status.SUCCEEDED
    assert run.events.filter(kind="infrastructure").count() == 1
    assert (tmp_path / "runs" / str(run.id) / "attempt-1" / "miniwdl.log").is_file()
    assert (tmp_path / "runs" / str(run.id) / "attempt-2" / "result.json").is_file()


@pytest.fixture
def analysis_workspace(settings, tmp_path):
    rawdata = tmp_path / "rawdata"
    databases = tmp_path / "databases"
    runs = tmp_path / "runs"
    _write_fastq(rawdata / "run_SAMPLE01_L01_R1.fq.gz", 1)
    _write_fastq(rawdata / "run_SAMPLE01_L01_R2.fq.gz", 2)

    directories = {
        "reference": "hg19/reference",
        "humandb": "hg19/humandb",
        "localdb": "hg19/local",
        "resource": "hg19/resource",
        "database": "hg19/database",
        "cnvdb": "hg19/cnvdb",
    }
    for relative in directories.values():
        (databases / relative).mkdir(parents=True)
    (databases / "hg19/reference/hg19.simp.fa").write_text(">chr1\nA\n", encoding="utf-8")
    (databases / "hg19/reference/hg19.simp.fa.fai").write_text(
        "chr1\t1\t6\t1\t2\n",
        encoding="utf-8",
    )
    panel_files = {
        "bed": "task_resource/panel.bed",
        "gene_list": "task_resource/genes.txt",
        "tert_bed": "task_resource/tert.bed",
        "p1q19_bed": "task_resource/1p19q.bed",
        "druggable_region": "task_resource/druggable.csv",
    }
    for relative in panel_files.values():
        path = databases / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test\n", encoding="utf-8")
    cnvkit = databases / "task_resource/cnvkit"
    cnvkit.mkdir(parents=True)
    catalog = {
        "schema_version": 1,
        "references": [
            {
                "id": "hg19",
                "name": "hg19",
                "ref_version": "hg19",
                "directories": directories,
                "required": [
                    {"path": "hg19/reference", "kind": "directory", "label": "reference"}
                ],
            }
        ],
        "panels": [
            {
                "id": "panel",
                "name": "Panel",
                **panel_files,
                "cnvkit_db": "task_resource/cnvkit",
                "required": [
                    {"path": "task_resource/panel.bed", "kind": "file", "label": "panel"}
                ],
            }
        ],
    }
    catalog_path = databases / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    settings.ANALYSIS_RAWDATA_ROOT = rawdata
    settings.ANALYSIS_RAWDATA_EXECUTION_ROOT = rawdata
    settings.ANALYSIS_DATABASE_ROOT = databases
    settings.ANALYSIS_DATABASE_EXECUTION_ROOT = databases
    settings.ANALYSIS_DATABASE_CATALOG = catalog_path
    settings.ANALYSIS_RUN_ROOT = runs
    return rawdata, databases, runs, catalog


def test_catalog_discovers_fastq_pair_and_reports_managed_workflow(client, analysis_workspace):
    _asset("solidtumorsingle", "SolidTumorSingle")

    response = client.get("/api/v1/analysis/catalog")

    assert response.status_code == 200
    assert len(response.data["datasets"]) == 1
    dataset = response.data["datasets"][0]
    assert dataset["name"] == "SAMPLE01"
    assert [item["mate"] for item in dataset["files"]] == [1, 2]
    workflow = next(
        item for item in response.data["workflows"] if item["slug"] == "solidtumorsingle"
    )
    assert workflow["ready"] is True
    assert response.data["database"]["references"][0]["ready"] is True


def test_create_run_fixes_revision_and_inputs(client, analysis_workspace):
    asset, revision = _asset("solidtumorsingle", "SolidTumorSingle")
    catalog = client.get("/api/v1/analysis/catalog").data
    dataset_id = catalog["datasets"][0]["id"]

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": "solidtumorsingle",
            "dataset": dataset_id,
            "reference": "hg19",
            "panel": "panel",
            "sample_id": "SAMPLE01",
            "sample_name": "示例样本",
            "sample_type": "tissue",
            "sample_gender": "女",
        },
        format="json",
    )

    assert response.status_code == 201
    run = AnalysisRun.objects.get(pk=response.data["id"])
    assert run.asset == asset
    assert run.revision == revision
    assert run.status == AnalysisRun.Status.QUEUED
    assert run.input_values["SolidTumorSingle.fastq1"].endswith("_R1.fq.gz")
    assert run.input_values["Collect.database"].endswith("hg19/database")
    assert run.input_values["Collect.fasta"].endswith("hg19/reference/hg19.simp.fa")
    assert run.input_values["Collect.fasta_fai"].endswith(
        "hg19/reference/hg19.simp.fa.fai"
    )
    assert run.events.get().message == "运行已进入队列。"


def test_create_run_uses_execution_roots_for_host_docker(
    client, analysis_workspace, settings
):
    _asset("solidtumorsingle", "SolidTumorSingle")
    settings.ANALYSIS_RAWDATA_EXECUTION_ROOT = Path("/mnt/nas/workspace/rawdata")
    settings.ANALYSIS_DATABASE_EXECUTION_ROOT = Path("/mnt/nas/workspace/databases")
    catalog = client.get("/api/v1/analysis/catalog").data

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": "solidtumorsingle",
            "dataset": catalog["datasets"][0]["id"],
            "reference": "hg19",
            "panel": "panel",
        },
        format="json",
    )

    assert response.status_code == 201
    inputs = AnalysisRun.objects.get(pk=response.data["id"]).input_values
    assert inputs["SolidTumorSingle.fastq1"].startswith(
        "/mnt/nas/workspace/rawdata/"
    )
    assert inputs["CallFusion.druggable_region"] == (
        "/mnt/nas/workspace/databases/task_resource/druggable.csv"
    )


def test_create_run_records_authenticated_submitter(client, analysis_workspace):
    _asset("solidtumorsingle", "SolidTumorSingle")
    user = get_user_model().objects.create_user(
        username="chaohuaiyu",
        password="chaohuaiyu",
    )
    client.force_login(user)
    catalog = client.get("/api/v1/analysis/catalog").data

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": "solidtumorsingle",
            "dataset": catalog["datasets"][0]["id"],
            "reference": "hg19",
            "panel": "panel",
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["actor"] == "chaohuaiyu"
    assert AnalysisRun.objects.get(pk=response.data["id"]).actor == "chaohuaiyu"


def test_create_run_normalizes_english_gender_for_legacy_wdl(client, analysis_workspace):
    _asset("solidtumorsingle", "SolidTumorSingle")
    catalog = client.get("/api/v1/analysis/catalog").data

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": "solidtumorsingle",
            "dataset": catalog["datasets"][0]["id"],
            "reference": "hg19",
            "panel": "panel",
            "sample_gender": "female",
        },
        format="json",
    )

    assert response.status_code == 201
    run = AnalysisRun.objects.get(pk=response.data["id"])
    assert run.input_values["SolidTumorSingle.sample_gender"] == "女"
    assert run.request_payload["sample_gender"] == "女"


def test_create_run_rejects_missing_database_before_queue(client, analysis_workspace):
    _asset("solidtumorsingle", "SolidTumorSingle")
    dataset_id = client.get("/api/v1/analysis/catalog").data["datasets"][0]["id"]
    _, databases, _, catalog = analysis_workspace
    catalog["references"][0]["required"].append(
        {"path": "hg19/reference/missing.fa", "kind": "file", "label": "missing"}
    )
    (databases / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": "solidtumorsingle",
            "dataset": dataset_id,
            "reference": "hg19",
            "panel": "panel",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_DATABASE_INCOMPLETE"
    assert response.data["error"]["details"]["missing"][0]["path"].endswith("missing.fa")
    assert AnalysisRun.objects.count() == 0


def test_catalog_accepts_declared_database_file_alternative(client, analysis_workspace):
    _asset("solidtumorsingle", "SolidTumorSingle")
    _, databases, _, catalog = analysis_workspace
    alternative = databases / "hg19/humandb/database.txt.gz"
    alternative.write_text("test\n", encoding="utf-8")
    catalog["references"][0]["required"].append(
        {
            "path": "hg19/humandb/database.txt",
            "alternatives": ["hg19/humandb/database.txt.gz"],
            "kind": "file",
            "label": "compressed database",
        }
    )
    (databases / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")

    response = client.get("/api/v1/analysis/catalog")

    reference = response.data["database"]["references"][0]
    assert response.status_code == 200
    assert reference["ready"] is True
    assert reference["requirements"][-1]["present"] is True


def test_run_detail_and_output_download_only_use_recorded_output_key(client, analysis_workspace):
    asset, revision = _asset("solidtumorsingle", "SolidTumorSingle")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "result-run"
    output = run_directory / "out" / "summary.txt"
    output.parent.mkdir(parents=True)
    output.write_text("done\n", encoding="utf-8")
    miniwdl_events = [
        {"message": "workflow start", "source": "wdl.w:Test", "timestamp": 100.0},
        {
            "message": "task setup",
            "name": "QC",
            "source": "wdl.w:Test.t:call-QC",
            "timestamp": 101.0,
        },
        {
            "message": "done (cached)",
            "source": "wdl.w:Test.t:call-QC",
            "timestamp": 103.0,
        },
        {
            "message": "task setup",
            "name": "Collect",
            "source": "wdl.w:Test.t:call-Collect",
            "timestamp": 103.5,
        },
        {
            "message": "done",
            "source": "wdl.w:Test.t:call-Collect",
            "timestamp": 109.5,
        },
        {"message": "done", "source": "wdl.w:Test", "timestamp": 110.0},
    ]
    (run_directory / "miniwdl.log").write_text(
        "".join(json.dumps(event) + "\n" for event in miniwdl_events),
        encoding="utf-8",
    )
    started_at = timezone.now()
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SolidTumorSingle",
        sample_id="SAMPLE01",
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        work_directory=str(run_directory),
        outputs={"outputs": {"SolidTumorSingle.summary": str(output)}},
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=12),
    )

    detail = client.get(f"/api/v1/analysis-runs/{run.id}")
    assert detail.status_code == 200
    assert detail.data["outputs"][0]["name"] == "summary.txt"
    assert detail.data["timing"] == {
        "queue_seconds": pytest.approx(0, abs=1),
        "total_seconds": 12.0,
        "execution_seconds": 10.0,
        "task_seconds": 8.0,
        "cached_tasks": 1,
        "tasks": [
            {
                "id": "wdl.w:Test.t:call-QC",
                "name": "QC",
                "call": "call-QC",
                "status": "succeeded",
                "cached": True,
                "offset_seconds": 1.0,
                "duration_seconds": 2.0,
            },
            {
                "id": "wdl.w:Test.t:call-Collect",
                "name": "Collect",
                "call": "call-Collect",
                "status": "succeeded",
                "cached": False,
                "offset_seconds": 3.5,
                "duration_seconds": 6.0,
            },
        ],
    }

    downloaded = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "SolidTumorSingle.summary"},
    )
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == b"done\n"
    missing = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "../../etc/passwd"},
    )
    assert missing.status_code == 404

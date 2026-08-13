from __future__ import annotations

import gzip
import io
import json
import signal
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from compiler_core import canonical_digest, compile_workflow
from workflows.analysis_runtime import (
    GIB,
    _available_memory_bytes,
    _cleanup_swarm_services_for_run,
    _is_infrastructure_error,
    _LeaseHeartbeat,
    _result_error,
    _verify_run_resource_manifests,
    claim_next_run,
    execute_analysis_run,
)
from workflows.analysis_runs import _parse_miniwdl_timing
from workflows.rawdata_index import queue_rawdata_scan, run_rawdata_scan_batch
from workflows.models import (
    AnalysisRun,
    WDLAsset,
    WDLSourceFile,
    WDLSourceRevision,
    WorkflowDocument,
    WorkflowVersion,
)


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

    assert (
        _is_infrastructure_error("command failed with exit status 2", log_path) is False
    )


def test_lease_loss_escalates_from_term_to_kill(monkeypatch):
    signals = []

    class FakeProcess:
        pid = 1234

        def poll(self):
            return None

        def wait(self, timeout=None):
            if len(signals) == 1:
                raise subprocess.TimeoutExpired("miniwdl", timeout)
            return -signal.SIGKILL

    heartbeat = _LeaseHeartbeat(type("Run", (), {"id": "test", "lease_token": None})())
    heartbeat.process = FakeProcess()
    monkeypatch.setattr(
        "workflows.analysis_runtime.os.killpg",
        lambda pid, value: signals.append((pid, value)),
    )

    heartbeat._terminate_process()

    assert signals == [(1234, signal.SIGTERM), (1234, signal.SIGKILL)]


def test_heartbeat_fences_process_after_database_errors_exceed_lease(monkeypatch):
    finalized = []
    run = type(
        "Run",
        (),
        {
            "id": "run-1",
            "pk": "run-1",
            "lease_token": "lease-1",
            "work_directory": "",
        },
    )()
    heartbeat = _LeaseHeartbeat(run)
    heartbeat.lease_deadline = 0
    monkeypatch.setattr(
        "workflows.analysis_runtime._renew_lease",
        lambda run: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(
        "workflows.analysis_runtime._finalize_cancelled_run",
        lambda run_id, lease: finalized.append((run_id, lease)),
    )

    assert heartbeat._renew_or_expire() is False
    assert heartbeat.lease_lost.is_set()
    assert finalized == [("run-1", "lease-1")]


def test_swarm_cleanup_only_removes_service_mounted_under_run(tmp_path):
    run_directory = tmp_path / "run-1"
    run_directory.mkdir()

    class FakeService:
        def __init__(self, name, source):
            self.name = name
            self.id = name
            self.removed = False
            self.attrs = {
                "Spec": {
                    "Labels": {"miniwdl_run_id": "call-task"},
                    "TaskTemplate": {
                        "ContainerSpec": {"Mounts": [{"Source": str(source)}]}
                    },
                }
            }

        def remove(self):
            self.removed = True

    owned = FakeService("owned", run_directory / "attempt-1/work/task")
    other = FakeService("other", tmp_path / "another-run/work/task")

    class FakeClient:
        class Services:
            def list(self, **kwargs):
                assert kwargs["filters"] == {"label": "miniwdl_run_id"}
                return [owned, other]

        services = Services()

    removed, errors = _cleanup_swarm_services_for_run(
        run_directory, docker_client=FakeClient()
    )

    assert removed == ["owned"]
    assert errors == []
    assert owned.removed is True
    assert other.removed is False


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


def _published_fastq_workflow(*, include_annotation_directory=False):
    tool = {
        "schema_version": "1.0.0",
        "id": "copy_reads",
        "name": "copy_reads",
        "display_name": "Copy reads",
        "tool_version": "1.0.0",
        "container": {"engine": "docker", "image": "ubuntu:24.04"},
        "inputs": [
            {
                "name": "read1",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r1",
                "required": True,
            },
            {
                "name": "read2",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r2",
                "required": True,
            },
        ],
        "outputs": [
            {
                "name": "copied",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r1",
                "capture": {"mode": "path", "value": "copied.fq.gz"},
            }
        ],
        "command": {
            "shell": "bash",
            "strict_mode": True,
            "template": 'cp "~{read1}" copied.fq.gz\n',
        },
    }
    graph = {
        "schema_version": "1.0.0",
        "id": "published_fastq",
        "name": "Published FASTQ",
        "target": {
            "language": "wdl",
            "version": "1.0",
            "profile": "miniwdl-compatible",
        },
        "nodes": [
            {
                "id": "read1",
                "type": "workflow_input",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "bio.fastq.gz.r1",
                    "required": True,
                },
            },
            {
                "id": "read2",
                "type": "workflow_input",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "bio.fastq.gz.r2",
                    "required": True,
                },
            },
            {
                "id": "copy",
                "type": "tool",
                "tool_ref": {
                    "id": "copy_reads",
                    "tool_version": "1.0.0",
                    "spec_version": "1.0.0",
                    "digest": canonical_digest(tool),
                },
            },
            {
                "id": "copied",
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
                "id": "e1",
                "source": {"node_id": "read1", "port": "value"},
                "target": {"node_id": "copy", "port": "read1"},
            },
            {
                "id": "e2",
                "source": {"node_id": "read2", "port": "value"},
                "target": {"node_id": "copy", "port": "read2"},
            },
            {
                "id": "e3",
                "source": {"node_id": "copy", "port": "copied"},
                "target": {"node_id": "copied", "port": "value"},
            },
        ],
    }
    interface_inputs = [
        {
            "name": "read1",
            "label": "Read 1",
            "wdl_type": "File",
            "semantic_type": "bio.fastq.gz.r1",
            "required": True,
        },
        {
            "name": "read2",
            "label": "Read 2",
            "wdl_type": "File",
            "semantic_type": "bio.fastq.gz.r2",
            "required": True,
        },
    ]
    if include_annotation_directory:
        graph["nodes"].insert(
            2,
            {
                "id": "humandb",
                "type": "workflow_input",
                "port": {
                    "name": "value",
                    "wdl_type": "Directory",
                    "semantic_type": "bio.annotation.database_dir",
                    "required": True,
                },
            },
        )
        interface_inputs.append(
            {
                "name": "humandb",
                "label": "ANNOVAR 数据库",
                "wdl_type": "Directory",
                "semantic_type": "bio.annotation.database_dir",
                "required": True,
            }
        )
    document = WorkflowDocument.objects.create(
        slug="published-fastq",
        name="Published FASTQ",
        workflow_graph=graph,
        tool_specs=[tool],
    )
    validation, artifacts = compile_workflow(graph, [tool])
    assert validation["status"] == "valid"
    compiled_bundle = {
        "entrypoint": "workflow.wdl",
        "files": {
            item["name"]: item["content"]
            for item in artifacts
            if item.get("media_type") == "application/wdl"
        },
        "call_count": 1,
    }
    return WorkflowVersion.objects.create(
        workflow=document,
        version=1,
        name=document.name,
        semantic_digest="sha256:published",
        workflow_graph=graph,
        tool_specs=[tool],
        compiled_bundle=compiled_bundle,
        compiled_digest=canonical_digest(compiled_bundle),
        compiler_profile="compiler-core-v1",
        interface_contract={
            "contract_version": "1.0.0",
            "inputs": interface_inputs,
            "outputs": [],
        },
    )


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


def test_worker_requeues_expired_preparing_lease_before_claim(settings):
    asset, revision = _asset("lease-preparing", "LeasePreparing")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LeasePreparing",
        sample_id="SAMPLE01",
        status=AnalysisRun.Status.PREPARING,
        progress=5,
        started_at=timezone.now() - timedelta(minutes=10),
        attempt_count=1,
        lease_token=uuid.uuid4(),
        lease_expires_at=timezone.now() - timedelta(minutes=5),
    )
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 0

    claimed = claim_next_run()

    assert claimed.id == run.id
    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.PREPARING
    assert run.attempt_count == 2
    assert run.events.filter(kind="lease").count() == 1


def test_worker_marks_expired_running_lease_failed_without_retry(settings):
    asset, revision = _asset("lease-running", "LeaseRunning")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LeaseRunning",
        sample_id="SAMPLE01",
        status=AnalysisRun.Status.RUNNING,
        progress=40,
        work_directory="/tmp/already-started",
        lease_token=uuid.uuid4(),
        lease_expires_at=timezone.now() - timedelta(minutes=5),
    )
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 0

    assert claim_next_run() is None

    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.FAILED
    assert "没有自动重跑" in run.error
    assert run.lease_token is None


def test_execute_run_retries_infrastructure_failure_once(
    settings, tmp_path, monkeypatch
):
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
            self.finished = False

        def poll(self):
            return self.exit_code if self.finished else None

        def wait(self, timeout=None):
            del timeout
            self.finished = True
            return self.exit_code

    monkeypatch.setattr(
        "workflows.analysis_runtime.shutil.which", lambda _: "/bin/miniwdl"
    )
    monkeypatch.setattr("workflows.analysis_runtime.subprocess.Popen", FakeProcess)

    execute_analysis_run(run)

    run.refresh_from_db()
    assert calls == 2
    assert run.status == AnalysisRun.Status.SUCCEEDED
    assert run.events.filter(kind="infrastructure").count() == 1
    assert (tmp_path / "runs" / str(run.id) / "attempt-1" / "miniwdl.log").is_file()
    assert (tmp_path / "runs" / str(run.id) / "attempt-2" / "result.json").is_file()


def test_execute_run_terminates_process_when_event_recording_fails(
    settings, tmp_path, monkeypatch
):
    asset, revision = _asset("event-failure", "EventFailure")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="EventFailure",
        sample_id="SAMPLE01",
    )
    settings.ANALYSIS_RUN_ROOT = tmp_path / "runs"
    signals = []

    class FakeProcess:
        pid = 5678

        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.stderr = io.StringIO(
                '{"message":"task setup","task":"EventFailure"}\n'
            )

        def poll(self):
            return None

        def wait(self, timeout=None):
            if len(signals) == 1:
                raise subprocess.TimeoutExpired("miniwdl", timeout)
            return -signal.SIGKILL

    original_event = __import__(
        "workflows.analysis_runtime", fromlist=["_event"]
    )._event

    def fail_on_miniwdl_event(*args, **kwargs):
        if kwargs.get("kind") == "miniwdl":
            raise RuntimeError("database write failed")
        return original_event(*args, **kwargs)

    monkeypatch.setattr(
        "workflows.analysis_runtime.shutil.which", lambda _: "/bin/miniwdl"
    )
    monkeypatch.setattr("workflows.analysis_runtime.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("workflows.analysis_runtime._event", fail_on_miniwdl_event)
    monkeypatch.setattr(
        "workflows.analysis_runtime.os.killpg",
        lambda pid, value: signals.append((pid, value)),
    )

    with pytest.raises(RuntimeError, match="database write failed"):
        execute_analysis_run(run)

    assert signals == [(5678, signal.SIGTERM), (5678, signal.SIGKILL)]


def test_worker_rejects_input_replaced_after_submission(settings, tmp_path):
    rawdata = tmp_path / "rawdata"
    fastq = rawdata / "sample_R1.fastq.gz"
    fastq.parent.mkdir()
    fastq.write_bytes(b"first")
    stat = fastq.stat()
    settings.ANALYSIS_RAWDATA_ROOT = rawdata
    run = type(
        "Run",
        (),
        {
            "request_payload": {
                "input_resource_manifest": {
                    "schema_version": 1,
                    "files": [
                        {
                            "relative_path": fastq.name,
                            "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "device": stat.st_dev,
                            "inode": stat.st_ino,
                        }
                    ],
                }
            }
        },
    )()
    fastq.write_bytes(b"replacement")

    with pytest.raises(RuntimeError, match="排队后发生变化"):
        _verify_run_resource_manifests(run)


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
    (databases / "hg19/reference/hg19.simp.fa").write_text(
        ">chr1\nA\n", encoding="utf-8"
    )
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
                    {
                        "path": "hg19/reference",
                        "kind": "directory",
                        "label": "reference",
                    }
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
                    {
                        "path": "task_resource/panel.bed",
                        "kind": "file",
                        "label": "panel",
                    }
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
    settings.ANALYSIS_RUN_EXECUTION_ROOT = runs
    settings.RAWDATA_SCAN_BATCH_ENTRIES = 100
    settings.RAWDATA_SCAN_MAX_FILES = 100
    settings.RAWDATA_SCAN_MAX_ENTRIES = 100
    settings.RAWDATA_SCAN_MAX_DEPTH = 8
    settings.RAWDATA_SCAN_BATCH_SECONDS = 10
    settings.RAWDATA_SCAN_LEASE_SECONDS = 60
    scan, _ = queue_rawdata_scan(
        actor="test",
        trigger="fixture",
        root_value=rawdata,
    )
    for _ in range(20):
        scan = run_rawdata_scan_batch(rawdata) or scan
        scan.refresh_from_db()
        if scan.finished_at:
            break
    assert scan.status == "succeeded"
    return rawdata, databases, runs, catalog


def test_catalog_discovers_fastq_pair_and_reports_managed_workflow(
    client, analysis_workspace
):
    _asset("solidtumorsingle", "SolidTumorSingle")

    response = client.get("/api/v1/analysis/catalog")

    assert response.status_code == 200
    assert len(response.data["datasets"]) == 1
    dataset = response.data["datasets"][0]
    assert dataset["name"] == "SAMPLE01"
    assert [item["mate"] for item in dataset["files"]] == [1, 2]
    workflow = next(
        item
        for item in response.data["workflows"]
        if item["slug"] == "solidtumorsingle"
    )
    assert workflow["ready"] is True
    assert workflow["reference_status"]["hg19"]["ready"] is True
    assert workflow["panel_status"]["panel"]["ready"] is True
    assert response.data["database"]["references"][0]["ready"] is True


def test_catalog_exposes_limited_rawdata_scan_without_partial_datasets(
    client, analysis_workspace, monkeypatch
):
    monkeypatch.setattr(
        "workflows.analysis_runs.discover_fastq_catalog",
        lambda: {
            "scan_limited": True,
            "scanned_at": "2026-08-12T08:10:00+00:00",
            "issues": [
                {
                    "code": "RAWDATA_SCAN_LIMIT_REACHED",
                    "message": "本次扫描达到安全预算。",
                }
            ],
            "datasets": [
                {
                    "id": "partial",
                    "status": "scan_incomplete",
                }
            ],
        },
    )

    response = client.get("/api/v1/analysis/catalog")

    assert response.status_code == 200
    assert response.data["datasets"] == []
    assert response.data["rawdata_scan"] == {
        "limited": True,
        "scanned_at": "2026-08-12T08:10:00+00:00",
        "issues": [
            {
                "code": "RAWDATA_SCAN_LIMIT_REACHED",
                "message": "本次扫描达到安全预算。",
            }
        ],
    }


def test_catalog_scopes_resource_readiness_to_legacy_input_adapter(
    client, analysis_workspace
):
    _asset("solidtumorsingle", "SolidTumorSingle")
    _, databases, _, catalog = analysis_workspace
    catalog["panels"].append(
        {
            "id": "generic-panel",
            "name": "只有通用文件的 Panel",
            "required": [],
        }
    )
    (databases / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")

    response = client.get("/api/v1/analysis/catalog")

    assert response.status_code == 200
    generic = next(
        item
        for item in response.data["database"]["panels"]
        if item["id"] == "generic-panel"
    )
    assert generic["ready"] is True
    workflow = next(
        item
        for item in response.data["workflows"]
        if item["slug"] == "solidtumorsingle"
    )
    scoped = workflow["panel_status"]["generic-panel"]
    assert scoped["ready"] is False
    assert {item["binding"] for item in scoped["missing"]} == {
        "bed",
        "gene_list",
        "tert_bed",
        "p1q19_bed",
        "druggable_region",
        "cnvkit_db",
    }


def test_catalog_lists_production_blood_workflows_with_explicit_blockers(
    client, analysis_workspace
):
    _asset("tumor-blood-single-production", "TumorBloodSingle")
    _asset("tumor-blood-pair-production", "TumorBloodPair")

    response = client.get("/api/v1/analysis/catalog")

    assert response.status_code == 200
    workflows = {
        item["slug"]: item
        for item in response.data["workflows"]
        if item["slug"].startswith("tumor-blood-")
    }
    assert set(workflows) == {
        "tumor-blood-single-production",
        "tumor-blood-pair-production",
    }
    assert workflows["tumor-blood-single-production"]["ready"] is False
    assert workflows["tumor-blood-pair-production"]["mode"] == "paired"
    for workflow in workflows.values():
        assert "数据库 catalog 尚未配置 hg38 参考资源。" in workflow["blockers"]
        assert "正式流程的运行输入映射尚未配置。" in workflow["blockers"]
        assert workflow["required_reference"] == "hg38"
        assert workflow["input_adapter_status"] == {
            "status": "pending",
            "unresolved_inputs": [
                "sample_info",
                "sample_info_new",
                "sample_info_list",
                "sample_info_list_new",
                "output_dir",
            ],
            "external_resource_count": 0,
            "external_resource_examples": [],
        }
    assert not any(
        item["id"] == "hg38" for item in response.data["database"]["references"]
    )


def test_catalog_reports_external_resources_in_pending_blood_adapter(
    client, analysis_workspace
):
    asset, revision = _asset("tumor-blood-single-production", "TumorBloodSingle")
    content = revision.content + '\nString legacy = "/easygene_data/db/a.txt"\nString remote = "oss://bucket/b.txt"\n'
    WDLSourceRevision.objects.filter(pk=revision.pk).update(content=content)
    source_file = revision.files.get(is_entry=True)
    WDLSourceFile.objects.filter(pk=source_file.pk).update(content=content)

    workflow = next(
        item
        for item in client.get("/api/v1/analysis/catalog").data["workflows"]
        if item["slug"] == asset.slug
    )

    assert workflow["input_adapter_status"]["external_resource_count"] == 2
    assert any("2 个 OSS/历史绝对资源引用" in item for item in workflow["blockers"])


def test_resource_migration_merges_blood_requirements_into_existing_hg38(
    client, analysis_workspace
):
    _, databases, _, catalog = analysis_workspace
    catalog["references"].append(
        {
            "id": "hg38",
            "name": "Existing hg38",
            "ref_version": "hg38",
            "required": [
                {"path": "hg38/reference", "kind": "directory", "label": "reference"}
            ],
        }
    )
    (databases / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    call_command("migrate_resource_catalog", actor="pytest")

    response = client.get("/api/v1/analysis/catalog")

    hg38 = next(item for item in response.data["database"]["references"] if item["id"] == "hg38")
    paths = {item["path"] for item in hg38["requirements"]}
    assert "hg38/reference" in paths
    assert "hg38/blood_tumor/resource/20231220/rs.uniq-20231218.in" in paths
    assert "hg19/resource/tumor-gene-20241016.xlsx" in paths
    panels = {item["id"]: item for item in response.data["database"]["panels"]}
    assert set(panels) >= {"blood-84", "blood-396", "blood-624"}
    assert any(
        item.get("binding") == "bed" and item["reason"] == "unconfigured"
        for item in panels["blood-84"]["missing"]
    )


def test_catalog_and_submit_support_latest_published_workflow(
    client, analysis_workspace
):
    version = _published_fastq_workflow()
    catalog = client.get("/api/v1/analysis/catalog").data
    workflow = next(
        item
        for item in catalog["workflows"]
        if item["source_type"] == "workflow_version"
    )

    assert workflow["revision"] == version.version
    assert workflow["ready"] is True
    assert workflow["requires_reference"] is False
    assert workflow["graph_summary"] == {
        "node_count": 4,
        "edge_count": 3,
        "input_count": 2,
        "tool_count": 1,
        "subworkflow_count": 0,
        "output_count": 1,
        "tools": [{"id": "copy_reads", "name": "copy_reads", "version": "1.0.0"}],
        "subworkflows": [],
    }

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": workflow["slug"],
            "dataset": catalog["datasets"][0]["id"],
            "sample_id": "SMALL01",
            "sample_name": "小数据",
        },
        format="json",
    )

    assert response.status_code == 201
    run = AnalysisRun.objects.get(pk=response.data["id"])
    assert run.workflow_version == version
    assert run.asset is None and run.revision is None
    assert run.source_bundle["entrypoint"] == "workflow.wdl"
    assert run.source_digest.startswith("sha256:")
    assert run.request_payload["compiled_source_digest"] == run.source_digest
    assert run.request_payload["input_digest"].startswith("sha256:")
    assert run.input_values["published_fastq.read1"].endswith("_R1.fq.gz")
    assert response.data["workflow"]["graph_summary"]["tool_count"] == 1


def test_submit_refreshes_cached_fastq_identity(client, analysis_workspace):
    version = _published_fastq_workflow()
    catalog = client.get("/api/v1/analysis/catalog").data
    dataset = catalog["datasets"][0]
    rawdata, _, _, _ = analysis_workspace
    read1 = rawdata / dataset["files"][0]["relative_path"]
    _write_fastq(read1, 1)
    with gzip.open(read1, "at", encoding="ascii") as handle:
        handle.write("@read-2/1\nTGCA\n+\nIIII\n")
    current = read1.stat()

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": f"published:{version.workflow.slug}:{version.version}",
            "dataset": dataset["id"],
            "sample_id": "CACHE01",
            "sample_name": "缓存后修改",
        },
        format="json",
    )

    assert response.status_code == 201
    run = AnalysisRun.objects.get(pk=response.data["id"])
    manifest = run.request_payload["input_resource_manifest"]
    recorded = manifest["files"][0]
    assert recorded["size"] == current.st_size
    assert recorded["mtime_ns"] == current.st_mtime_ns
    assert recorded["inode"] == current.st_ino
    _verify_run_resource_manifests(run)


def test_catalog_adds_requested_historical_published_workflow(
    client, analysis_workspace
):
    first = _published_fastq_workflow()
    second = WorkflowVersion.objects.create(
        workflow=first.workflow,
        version=2,
        name="Published FASTQ v2",
        description=first.description,
        kind=first.kind,
        semantic_digest="sha256:published-v2",
        workflow_graph=first.workflow_graph,
        editor_document=first.editor_document,
        tool_specs=first.tool_specs,
        compiled_bundle=first.compiled_bundle,
        compiled_digest=first.compiled_digest,
        compiler_profile=first.compiler_profile,
        interface_contract=first.interface_contract,
        subworkflow_references=first.subworkflow_references,
    )

    default_catalog = client.get("/api/v1/analysis/catalog").data
    default_versions = [
        item["revision"]
        for item in default_catalog["workflows"]
        if item.get("source_slug") == first.workflow.slug
    ]
    assert default_versions == [second.version]

    requested_catalog = client.get(
        "/api/v1/analysis/catalog",
        {"workflow": first.workflow.slug, "revision": first.version},
    ).data
    requested_versions = [
        item["revision"]
        for item in requested_catalog["workflows"]
        if item.get("source_slug") == first.workflow.slug
    ]
    assert requested_versions == [first.version, second.version]
    historical = next(
        item
        for item in requested_catalog["workflows"]
        if item.get("source_slug") == first.workflow.slug
        and item["revision"] == first.version
    )
    assert historical["slug"] == f"published:{first.workflow.slug}:{first.version}"
    assert historical["ready"] is True


def test_published_annotation_workflow_injects_managed_database_directory(
    client, analysis_workspace
):
    _published_fastq_workflow(include_annotation_directory=True)
    catalog = client.get("/api/v1/analysis/catalog").data
    workflow = next(
        item
        for item in catalog["workflows"]
        if item["source_type"] == "workflow_version"
    )

    assert workflow["ready"] is True
    assert workflow["requires_reference"] is True
    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": workflow["slug"],
            "dataset": catalog["datasets"][0]["id"],
            "reference": "hg19",
            "sample_id": "ANNO01",
            "sample_type": "tissue",
            "sample_gender": "女",
        },
        format="json",
    )

    assert response.status_code == 201
    run = AnalysisRun.objects.get(pk=response.data["id"])
    assert run.input_values["published_fastq.humandb"].endswith("/hg19/humandb")
    assert run.request_payload["database_digest"].startswith("sha256:")
    assert run.request_payload["reference_name"] == "hg19"
    assert run.request_payload["panel_name"] is None
    assert run.request_payload["sample_type"] == "tissue"
    assert run.request_payload["sample_gender"] == "女"


def test_catalog_returns_workflow_scoped_reference_readiness(
    client, analysis_workspace
):
    _published_fastq_workflow(include_annotation_directory=True)
    _, databases, _, catalog_document = analysis_workspace
    catalog_document["references"][0]["required"].append(
        {
            "path": "hg19/cnvdb/not-used-by-annotation.txt",
            "kind": "file",
            "label": "未选择的 CNV 数据库",
        }
    )
    (databases / "catalog.json").write_text(
        json.dumps(catalog_document), encoding="utf-8"
    )

    catalog = client.get("/api/v1/analysis/catalog").data
    workflow = next(
        item
        for item in catalog["workflows"]
        if item["source_type"] == "workflow_version"
    )

    assert catalog["database"]["references"][0]["ready"] is False
    assert workflow["reference_status"]["hg19"]["ready"] is True
    assert workflow["reference_status"]["hg19"]["missing"] == []


def test_published_workflow_derives_input_names_from_graph_node_ids(
    client, analysis_workspace
):
    version = _published_fastq_workflow()
    WorkflowVersion.objects.filter(pk=version.pk).update(interface_contract={})
    catalog = client.get("/api/v1/analysis/catalog").data
    workflow = next(
        item
        for item in catalog["workflows"]
        if item["source_type"] == "workflow_version"
    )

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": workflow["slug"],
            "dataset": catalog["datasets"][0]["id"],
        },
        format="json",
    )

    assert response.status_code == 201
    assert set(AnalysisRun.objects.get(pk=response.data["id"]).input_values) == {
        "published_fastq.read1",
        "published_fastq.read2",
    }


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
    assert run.request_payload["database_catalog_digest"].startswith("sha256:")
    assert run.request_payload["reference_digest"].startswith("sha256:")
    assert run.request_payload["panel_digest"].startswith("sha256:")
    assert run.events.get().message == "运行已进入队列。"


def test_historical_wdl_revision_is_selected_and_frozen_explicitly(
    client, analysis_workspace
):
    asset, first = _asset("solidtumorsingle", "SolidTumorSingle")
    second = WDLSourceRevision.objects.create(
        asset=asset,
        version=2,
        operation=WDLSourceRevision.Operation.EDIT,
        content=first.content.replace("sample_out", "latest_sample_out"),
        digest="sha256:latest-revision",
        analysis=first.analysis,
    )
    WDLSourceFile.objects.create(
        revision=second,
        path="workflow.wdl",
        content=second.content,
        digest="sha256:latest-revision-file",
        is_entry=True,
    )

    catalog = client.get(
        "/api/v1/analysis/catalog",
        {"workflow": asset.slug, "revision": first.version},
    ).data
    managed = [
        item for item in catalog["workflows"] if item.get("source_slug") == asset.slug
    ]
    historical = next(item for item in managed if item["revision"] == first.version)
    latest = next(item for item in managed if item["revision"] == second.version)

    assert latest["slug"] == asset.slug
    assert historical["slug"] == f"wdl-asset:{asset.slug}:{first.version}"
    assert historical["digest"] == first.digest
    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": historical["slug"],
            "dataset": catalog["datasets"][0]["id"],
            "reference": "hg19",
            "panel": "panel",
            "sample_id": "HISTORY01",
            "sample_name": "历史修订验证",
            "sample_type": "tissue",
            "sample_gender": "女",
        },
        format="json",
    )

    assert response.status_code == 201
    run = AnalysisRun.objects.get(pk=response.data["id"])
    assert run.asset == asset
    assert run.revision == first
    assert run.request_payload["workflow"] == historical["slug"]
    assert run.request_payload["wdl_revision"] == first.version
    assert run.request_payload["wdl_revision_digest"] == first.digest
    assert response.data["workflow"]["revision"] == first.version
    assert response.data["workflow"]["digest"] == first.digest


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
    assert inputs["SolidTumorSingle.fastq1"].startswith("/mnt/nas/workspace/rawdata/")
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
    run = AnalysisRun.objects.get(pk=response.data["id"])
    assert run.actor == "chaohuaiyu"
    assert run.submitted_by == user


def test_regular_user_only_sees_own_runs(client, analysis_workspace):
    asset, revision = _asset("solidtumorsingle", "SolidTumorSingle")
    user = get_user_model().objects.create_user(
        username="chaohuaiyu",
        password="test-password",
    )
    own = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SolidTumorSingle",
        sample_id="OWN",
        actor="chaohuaiyu",
        submitted_by=user,
    )
    other = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SolidTumorSingle",
        sample_id="OTHER",
        actor="other-user",
    )
    client.force_login(user)

    response = client.get("/api/v1/analysis-runs")

    assert response.status_code == 200
    assert [item["id"] for item in response.data["results"]] == [str(own.id)]
    assert client.get(f"/api/v1/analysis-runs/{other.id}").status_code == 404


def test_create_run_normalizes_english_gender_for_legacy_wdl(
    client, analysis_workspace
):
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
    assert response.data["error"]["details"]["missing"][0]["path"].endswith(
        "missing.fa"
    )
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


def test_run_detail_and_output_download_only_use_recorded_output_key(
    client, analysis_workspace
):
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


def test_run_output_translates_host_execution_path_for_backend_mount(
    client, analysis_workspace, settings
):
    asset, revision = _asset("host-output", "HostOutput")
    _, _, runs, _ = analysis_workspace
    local_run = runs / "host-run"
    local_output = local_run / "out" / "result.txt"
    local_output.parent.mkdir(parents=True)
    local_output.write_text("host result\n", encoding="utf-8")
    host_root = Path("/mnt/nas/workspace/analysis-runs")
    settings.ANALYSIS_RUN_EXECUTION_ROOT = host_root
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="HostOutput",
        sample_id="HOST",
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        work_directory=str(host_root / "host-run"),
        outputs={
            "outputs": {"HostOutput.result": str(host_root / "host-run/out/result.txt")}
        },
    )

    detail = client.get(f"/api/v1/analysis-runs/{run.id}")
    downloaded = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "HostOutput.result"},
    )

    assert detail.data["outputs"][0]["kind"] == "file"
    assert detail.data["outputs"][0]["name"] == "result.txt"
    assert b"".join(downloaded.streaming_content) == b"host result\n"


def test_run_output_rejects_symlink_escape(client, analysis_workspace, tmp_path):
    asset, revision = _asset("symlink-output", "SymlinkOutput")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "symlink-run"
    run_directory.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    link = run_directory / "escaped.txt"
    link.symlink_to(outside)
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SymlinkOutput",
        sample_id="SYMLINK",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SymlinkOutput.result": str(link)}},
    )

    detail = client.get(f"/api/v1/analysis-runs/{run.id}")
    downloaded = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "SymlinkOutput.result"},
    )

    assert detail.data["outputs"][0]["kind"] == "value"
    assert downloaded.status_code == 404

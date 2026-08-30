from __future__ import annotations

import gzip
import io
import json
import os
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from compiler_core import canonical_digest, compile_workflow
import workflows.analysis_runs as analysis_runs_module
from workflows.analysis_runtime import (
    GIB,
    _available_memory_bytes,
    _cleanup_swarm_services_for_run,
    _failure_metadata,
    _is_infrastructure_error,
    _LeaseHeartbeat,
    _read_result_json,
    _result_error,
    _verify_run_resource_manifests,
    claim_next_run,
    execute_analysis_run,
)
from workflows.analysis_runs import (
    AnalysisInputError,
    _catalog_resource_manifest,
    _parse_miniwdl_timing,
)
from workflows.integration_outputs import (
    _directory_manifest,
    _directory_manifest_isolated,
    _sha256,
    ResourceSnapshotBudget,
    backfill_output_manifest,
    build_output_manifest,
    open_verified_output,
    output_manifest_is_current,
    output_value_limit_reason,
)
from workflows.rawdata_index import queue_rawdata_scan, run_rawdata_scan_batch
from workflows.models import (
    AnalysisRun,
    WDLAsset,
    WDLSourceFile,
    WDLSourceRevision,
    WorkflowDocument,
    WorkflowVersion,
)


pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("auth_disabled")]


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


def test_result_json_read_is_bounded(settings, tmp_path):
    settings.ANALYSIS_RESULT_JSON_MAX_BYTES = 8
    result_path = tmp_path / "result.json"
    result_path.write_text('{"outputs": {}}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="result.json 超过安全上限"):
        _read_result_json(result_path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_result_json_rejects_non_finite_numbers(tmp_path, constant):
    result_path = tmp_path / "result.json"
    result_path.write_text(
        f'{{"outputs": {{"value": {constant}}}}}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="不是有效 JSON"):
        _read_result_json(result_path)


def test_inline_output_values_and_empty_legacy_containers_are_bounded(settings):
    settings.ANALYSIS_OUTPUT_VALUE_MAX_BYTES = 16
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS = 8

    assert output_value_limit_reason("x" * 100) == "output_value_size_exceeded"
    assert output_value_limit_reason(float("nan")) == "output_value_type_invalid"

    flattened = analysis_runs_module._flatten_outputs(
        {"values": [[] for _ in range(100)]}
    )
    assert len(flattened) <= 8
    assert flattened[-1][0] == "<truncated>"
    assert flattened[-1][1].reason == "output_snapshot_item_limit_exceeded"


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
                output = Path(result_path).parents[1] / "final.txt"
                output.write_text("completed\n", encoding="utf-8")
                with open(result_path, "w", encoding="utf-8") as handle:
                    json.dump({"outputs": {"Retryable.result": str(output)}}, handle)
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
    assert run.output_status == AnalysisRun.OutputStatus.COMPLETE
    assert run.output_manifest["integrity_version"] == 2
    output_item = run.output_manifest["items"][0]
    assert output_item["kind"] == "file"
    assert output_item["sha256"].startswith("sha256:")
    assert Path(output_item["path"]).is_file()
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


def test_worker_rejects_legacy_file_manifest_without_trusted_digest(
    settings, tmp_path
):
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
    with pytest.raises(RuntimeError, match="完整性证据过旧"):
        _verify_run_resource_manifests(run)

    assert _failure_metadata("受管资源完整性证据过旧，请重新投递任务") == {
        "code": "ANALYSIS_RESOURCE_MANIFEST_OUTDATED",
        "category": "resource",
        "retryable": False,
    }


def test_worker_rejects_file_when_ancestor_is_replaced_by_symlink(
    settings, tmp_path
):
    rawdata = tmp_path / "rawdata"
    lane = rawdata / "lane"
    lane.mkdir(parents=True)
    resource = lane / "sample.fastq.gz"
    resource.write_bytes(b"content")
    observed = resource.stat()
    settings.ANALYSIS_RAWDATA_ROOT = rawdata
    run = type(
        "Run",
        (),
        {
            "request_payload": {
                "input_resource_manifest": {
                    "schema_version": 2,
                    "files": [
                        {
                            "relative_path": "lane/sample.fastq.gz",
                            "kind": "file",
                            "verification": "identity_v2",
                            "size": observed.st_size,
                            "mtime_ns": observed.st_mtime_ns,
                            "ctime_ns": observed.st_ctime_ns,
                            "device": observed.st_dev,
                            "inode": observed.st_ino,
                        }
                    ],
                }
            }
        },
    )()
    moved = tmp_path / "moved-lane"
    lane.rename(moved)
    lane.symlink_to(moved, target_is_directory=True)

    with pytest.raises(RuntimeError, match="受管资源已不存在"):
        _verify_run_resource_manifests(
            run,
            snapshot_budget=ResourceSnapshotBudget(deadline_seconds=2),
        )


def test_worker_rejects_directory_content_changed_with_original_stat(
    settings, tmp_path
):
    database = tmp_path / "database"
    resource = database / "reference"
    resource.mkdir(parents=True)
    child = resource / "reference.fa"
    child.write_bytes(b"first\n")
    settings.ANALYSIS_DATABASE_ROOT = database
    recorded = _directory_manifest(resource)
    resource_stat = resource.stat()
    run = type(
        "Run",
        (),
        {
            "request_payload": {
                "database_resource_manifest": {
                    "schema_version": 1,
                    "resources": [
                        {
                            "relative_path": "reference",
                            "kind": "directory",
                            "verification": "directory_identity_sha256",
                            "digest": recorded["digest"],
                            "identity": {
                                "mtime_ns": resource_stat.st_mtime_ns,
                                "ctime_ns": resource_stat.st_ctime_ns,
                                "device": resource_stat.st_dev,
                                "inode": resource_stat.st_ino,
                            },
                        }
                    ],
                }
            }
        },
    )()

    child_stat = child.stat()
    time.sleep(0.01)
    child.write_bytes(b"second")
    os.utime(child, ns=(child_stat.st_atime_ns, child_stat.st_mtime_ns))

    with pytest.raises(RuntimeError, match="目录校验和不匹配"):
        _verify_run_resource_manifests(run)


def test_directory_manifest_handles_deep_tree_without_python_recursion(
    settings, tmp_path: Path
):
    settings.ANALYSIS_RESOURCE_MANIFEST_MAX_ENTRIES = 2_000
    settings.ANALYSIS_RESOURCE_MANIFEST_MAX_DEPTH = 2_000
    root = tmp_path / "deep-tree"
    root.mkdir()
    directories = []
    current = root
    try:
        for _ in range(1_050):
            current = current / "d"
            current.mkdir()
            directories.append(current)

        manifest = _directory_manifest(root)

        assert manifest["entry_count"] == 1_050
        assert manifest["digest"].startswith("sha256:")
    finally:
        for directory in reversed(directories):
            directory.rmdir()
        root.rmdir()


def test_directory_manifest_rejects_excessive_depth(settings, tmp_path: Path):
    settings.ANALYSIS_RESOURCE_MANIFEST_MAX_DEPTH = 2
    root = tmp_path / "bounded-depth"
    leaf = root / "one" / "two" / "three"
    leaf.mkdir(parents=True)

    with pytest.raises(ValueError, match="目录深度超过安全上限"):
        _directory_manifest(root)


def test_request_directory_manifest_timeout_kills_isolated_process(
    tmp_path: Path,
):
    original_popen = subprocess.Popen
    processes = []

    def start_sleeping_process(_command, **kwargs):
        process = original_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **kwargs,
        )
        processes.append(process)
        return process

    started_at = time.monotonic()
    with (
        patch(
            "workflows.integration_outputs.subprocess.Popen",
            side_effect=start_sleeping_process,
        ),
        pytest.raises(ValueError, match="目录快照超过时间上限"),
    ):
        _directory_manifest_isolated(tmp_path, timeout_seconds=0.05)

    assert time.monotonic() - started_at < 1
    assert len(processes) == 1
    for _ in range(1_000):
        if processes[0].poll() is not None:
            break
        time.sleep(0.001)
    assert processes[0].returncode is not None


def test_catalog_directory_identity_digest_is_verified(settings, tmp_path: Path):
    databases = tmp_path / "databases"
    bundle = databases / "hg19" / "bundle"
    bundle.mkdir(parents=True)
    child = bundle / "reference.fa"
    child.write_text(">chr1\nACGT\n", encoding="utf-8")
    settings.ANALYSIS_DATABASE_ROOT = databases
    expected = _directory_manifest(bundle)["digest"]
    entry = {
        "id": "hg19",
        "required": [
            {
                "path": "hg19/bundle",
                "kind": "directory",
                "identity_digest": expected.removeprefix("sha256:"),
            }
        ],
    }

    manifest = _catalog_resource_manifest(entry)

    assert manifest["resources"][0]["catalog_identity_digest"] == expected.removeprefix(
        "sha256:"
    )

    entry["required"][0]["identity_digest"] = "0" * 64
    with pytest.raises(AnalysisInputError, match="身份摘要不匹配"):
        _catalog_resource_manifest(entry)


def test_worker_rejects_catalog_checksum_that_differs_from_observed_content(
    settings, tmp_path
):
    database = tmp_path / "database"
    resource = database / "reference.fa"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b">chr1\nACGT\n")
    stat = resource.stat()
    settings.ANALYSIS_DATABASE_ROOT = database
    run = type(
        "Run",
        (),
        {
            "request_payload": {
                "database_resource_manifest": {
                    "schema_version": 1,
                    "resources": [
                        {
                            "relative_path": resource.name,
                            "kind": "file",
                            "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "device": stat.st_dev,
                            "inode": stat.st_ino,
                            "sha256": _sha256(resource),
                            "catalog_sha256": "0" * 64,
                        }
                    ],
                }
            }
        },
    )()

    with pytest.raises(RuntimeError, match="目录声明的校验和不匹配"):
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


def test_browser_submit_rejects_empty_fastq_header(client, analysis_workspace):
    version = _published_fastq_workflow()
    catalog = client.get("/api/v1/analysis/catalog").data
    dataset = catalog["datasets"][0]
    rawdata, _, _, _ = analysis_workspace
    read1_item = next(item for item in dataset["files"] if item["mate"] == 1)
    with gzip.open(rawdata / read1_item["relative_path"], "wt", encoding="ascii") as handle:
        handle.write("@\nACGT\n+\n!!!!\n")

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": f"published:{version.workflow.slug}:{version.version}",
            "dataset": dataset["id"],
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_FASTQ_INVALID"


def test_browser_submit_rejects_fastq_replaced_by_directory(
    client, analysis_workspace
):
    version = _published_fastq_workflow()
    catalog = client.get("/api/v1/analysis/catalog").data
    dataset = catalog["datasets"][0]
    rawdata, _, _, _ = analysis_workspace
    read1_item = next(item for item in dataset["files"] if item["mate"] == 1)
    read1 = rawdata / read1_item["relative_path"]
    read1.unlink()
    read1.mkdir()

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": f"published:{version.workflow.slug}:{version.version}",
            "dataset": dataset["id"],
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_FASTQ_INVALID"


def test_browser_submit_rejects_oversized_fastq_line(
    client, analysis_workspace, settings
):
    version = _published_fastq_workflow()
    catalog = client.get("/api/v1/analysis/catalog").data
    dataset = catalog["datasets"][0]
    rawdata, _, _, _ = analysis_workspace
    read1_item = next(item for item in dataset["files"] if item["mate"] == 1)
    settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS = 16
    with gzip.open(rawdata / read1_item["relative_path"], "wt", encoding="ascii") as handle:
        handle.write(f"@{'x' * 32}/1\nACGT\n+\n!!!!\n")

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": f"published:{version.workflow.slug}:{version.version}",
            "dataset": dataset["id"],
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_FASTQ_RECORD_TOO_LARGE"


def test_browser_submit_rejects_oversized_gzip_header(
    client, analysis_workspace, settings
):
    version = _published_fastq_workflow()
    catalog = client.get("/api/v1/analysis/catalog").data
    dataset = catalog["datasets"][0]
    rawdata, _, _, _ = analysis_workspace
    read1_item = next(item for item in dataset["files"] if item["mate"] == 1)
    settings.ANALYSIS_INPUT_GZIP_HEADER_MAX_BYTES = 32
    header_with_unterminated_name = b"\x1f\x8b\x08\x08" + b"\x00" * 6
    (rawdata / read1_item["relative_path"]).write_bytes(
        header_with_unterminated_name + b"x" * 4096
    )

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": f"published:{version.workflow.slug}:{version.version}",
            "dataset": dataset["id"],
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_FASTQ_INVALID"


def test_browser_submit_rejects_fastq_record_in_second_gzip_member(
    client, analysis_workspace, settings
):
    version = _published_fastq_workflow()
    catalog = client.get("/api/v1/analysis/catalog").data
    dataset = catalog["datasets"][0]
    rawdata, _, _, _ = analysis_workspace
    read1_item = next(item for item in dataset["files"] if item["mate"] == 1)
    settings.ANALYSIS_INPUT_GZIP_HEADER_MAX_BYTES = 32
    oversized_second_header = b"\x1f\x8b\x08\x08" + b"\x00" * 6 + b"x" * 4096
    (rawdata / read1_item["relative_path"]).write_bytes(
        gzip.compress(b"") + oversized_second_header
    )

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": f"published:{version.workflow.slug}:{version.version}",
            "dataset": dataset["id"],
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_FASTQ_INVALID"


def test_browser_submit_rejects_fastq_changed_during_validation(
    client, analysis_workspace, monkeypatch
):
    version = _published_fastq_workflow()
    catalog = client.get("/api/v1/analysis/catalog").data
    dataset = catalog["datasets"][0]
    rawdata, _, _, _ = analysis_workspace
    read1_item = next(item for item in dataset["files"] if item["mate"] == 1)
    read1 = rawdata / read1_item["relative_path"]
    original_open = analysis_runs_module._open_regular_readonly

    class ReplacingHandle:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.handle.close()
            read1.write_bytes(b"changed")
            return False

    def open_with_replacement(path, **kwargs):
        handle = original_open(path, **kwargs)
        return ReplacingHandle(handle) if path == read1 else handle

    monkeypatch.setattr(
        analysis_runs_module,
        "_open_regular_readonly",
        open_with_replacement,
    )

    response = client.post(
        "/api/v1/analysis-runs",
        {
            "workflow": f"published:{version.workflow.slug}:{version.version}",
            "dataset": dataset["id"],
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_RESOURCE_CHANGED"


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
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert error is None
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["output_manifest", "output_status", "updated_at"])

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


def test_incomplete_v2_manifest_keeps_verified_file_downloadable_without_legacy_fallback(
    client, analysis_workspace, settings, monkeypatch
):
    asset, revision = _asset("partial-output", "PartialOutput")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "partial-output"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("verified\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="PartialOutput",
        sample_id="PARTIAL",
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        work_directory=str(run_directory),
        request_payload={
            "integration_output_contract": [
                {
                    "key": "PartialOutput.result",
                    "wdl_type": "File",
                    "required": True,
                },
                {
                    "key": "PartialOutput.note",
                    "wdl_type": "String",
                    "required": True,
                },
            ]
        },
        outputs={
            "outputs": {
                "PartialOutput.result": str(output),
                "PartialOutput.note": "sensitive-marker-" + "x" * 100,
            }
        },
    )
    settings.ANALYSIS_OUTPUT_VALUE_MAX_BYTES = 16
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["output_manifest", "output_status", "updated_at"])
    monkeypatch.setattr(
        "workflows.analysis_runs._flatten_outputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v2 manifest must not read legacy outputs")
        ),
    )

    listed = client.get("/api/v1/analysis-runs")
    detail = client.get(f"/api/v1/analysis-runs/{run.id}")
    downloaded = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "PartialOutput.result"},
    )
    incomplete = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "PartialOutput.note"},
    )

    assert listed.status_code == 200
    assert listed.data["view"] == "summary"
    assert listed.data["results"][0]["outputs"] == []
    assert "sensitive-marker" not in str(listed.data)
    outputs = {item["key"]: item for item in detail.data["outputs"]}
    assert outputs["PartialOutput.result"]["download_url"]
    assert outputs["PartialOutput.note"] == {
        "key": "PartialOutput.note",
        "kind": "unverifiable",
        "name": "PartialOutput.note",
        "reason": "output_value_size_exceeded",
    }
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == b"verified\n"
    assert incomplete.status_code == 409
    assert incomplete.data["error"]["code"] == "ANALYSIS_OUTPUT_INCOMPLETE"


def test_browser_output_download_rejects_tampered_persisted_manifest(
    client, analysis_workspace
):
    asset, revision = _asset("manifest-output", "ManifestOutput")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "manifest-run"
    run_directory.mkdir(parents=True)
    output = run_directory / "summary.txt"
    output.write_text("original\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="ManifestOutput",
        sample_id="MANIFEST",
        actor="local-user",
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        work_directory=str(run_directory),
        outputs={
            "outputs": {
                "ManifestOutput.summary": str(output),
                "ManifestOutput.optional": None,
            }
        },
    )
    manifest, output_status, error = build_output_manifest(
        run, run.outputs
    )
    assert error is None
    assert output_status == AnalysisRun.OutputStatus.COMPLETE
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["output_manifest", "output_status", "updated_at"])

    detail = client.get(f"/api/v1/analysis-runs/{run.id}")
    assert detail.status_code == 200
    assert next(
        item
        for item in detail.data["outputs"]
        if item["key"] == "ManifestOutput.optional"
    )["value"] is None
    item = next(
        item
        for item in detail.data["outputs"]
        if item["key"] == "ManifestOutput.summary"
    )
    downloaded = client.get(item["download_url"])
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == b"original\n"

    output_stat = output.stat()
    output.write_text("tampered\n", encoding="utf-8")
    os.utime(output, ns=(output_stat.st_atime_ns, output_stat.st_mtime_ns))
    changed = client.get(item["download_url"])
    assert changed.status_code == 409
    assert changed.data["error"]["code"] == "ANALYSIS_OUTPUT_CHANGED"


def test_output_snapshot_is_reused_and_stream_handle_is_race_safe(
    client, analysis_workspace
):
    asset, revision = _asset("snapshot-reuse", "SnapshotReuse")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-reuse"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("immutable\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotReuse",
        sample_id="REUSE",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={
            "outputs": {
                "SnapshotReuse.first": str(output),
                "SnapshotReuse.second": str(output),
            }
        },
    )

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert error is None
    file_items = [item for item in manifest["items"] if item["kind"] == "file"]
    assert len(file_items) == 2
    assert file_items[0]["path"] == file_items[1]["path"]
    assert file_items[0]["identity"] == file_items[1]["identity"]
    _, handle = open_verified_output(file_items[0], run_root=run.work_directory)
    output.write_text("changed after open\n", encoding="utf-8")
    try:
        assert handle.read() == b"immutable\n"
    finally:
        handle.close()

    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["output_manifest", "output_status", "updated_at"])
    for item in file_items:
        response = client.get(
            f"/api/v1/analysis-runs/{run.id}/outputs",
            {"key": item["key"]},
        )
        assert response.status_code == 409


def test_output_download_rejects_tampered_snapshot(client, analysis_workspace):
    asset, revision = _asset("snapshot-tamper", "SnapshotTamper")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-tamper"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("trusted\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotTamper",
        sample_id="TAMPER",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotTamper.result": str(output)}},
    )
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert error is None
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["output_manifest", "output_status", "updated_at"])
    snapshot = Path(manifest["items"][0]["path"])
    snapshot.chmod(0o644)
    snapshot.write_text("attacker\n", encoding="utf-8")

    response = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "SnapshotTamper.result"},
    )

    assert response.status_code == 409
    assert response.data["error"]["code"] == "ANALYSIS_OUTPUT_CHANGED"


def test_output_snapshot_limits_report_specific_incomplete_reason(
    analysis_workspace, settings, monkeypatch
):
    asset, revision = _asset("snapshot-limits", "SnapshotLimits")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-limits"
    run_directory.mkdir(parents=True)
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotLimits",
        sample_id="LIMIT",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotLimits.values": [1, 2, 3]}},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS = 2

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    assert manifest["unverifiable_outputs"] == [
        {
            "key": "SnapshotLimits.values[1]",
            "reason": "output_snapshot_item_limit_exceeded",
        }
    ]

    output = run_directory / "large.bin"
    output.write_bytes(b"1234")
    run.outputs = {"outputs": {"SnapshotLimits.file": str(output)}}
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS = 256
    monkeypatch.setattr(
        "workflows.integration_outputs.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )
    manifest, output_status, _ = build_output_manifest(run, run.outputs)
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert manifest["unverifiable_outputs"][0]["reason"] == (
        "output_snapshot_storage_insufficient"
    )


def test_output_snapshot_copy_is_bounded_when_source_grows(
    analysis_workspace, settings, monkeypatch
):
    asset, revision = _asset("snapshot-growth", "SnapshotGrowth")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-growth"
    run_directory.mkdir(parents=True)
    output = run_directory / "growing.bin"
    output.write_bytes(b"a" * (2 * 1024 * 1024))
    initial_size = output.stat().st_size
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotGrowth",
        sample_id="GROWTH",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotGrowth.file": str(output)}},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES = 0
    original_open = analysis_runs_module._open_regular_readonly
    bytes_read = 0

    class GrowingHandle:
        def __init__(self, handle):
            self.handle = handle
            self.appended = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            nonlocal bytes_read
            data = self.handle.read(size)
            bytes_read += len(data)
            if not self.appended:
                with output.open("ab") as writer:
                    writer.write(b"b" * (4 * 1024 * 1024))
                self.appended = True
            return data

        def seek(self, *args):
            return self.handle.seek(*args)

    def open_with_growth(path, **kwargs):
        handle = original_open(path, **kwargs)
        return GrowingHandle(handle) if Path(path) == output else handle

    monkeypatch.setattr(
        "workflows.integration_outputs._open_regular_readonly",
        open_with_growth,
    )

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    assert manifest["unverifiable_outputs"][0]["reason"] == "file_digest_failed"
    assert bytes_read <= initial_size + 1
    assert not list((run_directory / ".verified-outputs").glob(".snapshot-*"))


def test_output_snapshot_rejects_atomic_source_replacement(
    analysis_workspace, settings, monkeypatch
):
    asset, revision = _asset("snapshot-replace", "SnapshotReplace")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-replace"
    run_directory.mkdir(parents=True)
    output = run_directory / "output.bin"
    output.write_bytes(b"a" * (2 * 1024 * 1024))
    replacement = run_directory / "replacement.bin"
    replacement.write_bytes(b"b" * output.stat().st_size)
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotReplace",
        sample_id="REPLACE",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotReplace.file": str(output)}},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES = 0
    original_open = analysis_runs_module._open_regular_readonly
    replaced = False

    class ReplacingHandle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            nonlocal replaced
            data = self.handle.read(size)
            if data and not replaced:
                os.replace(replacement, output)
                replaced = True
            return data

    def open_with_replacement(path, **kwargs):
        handle = original_open(path, **kwargs)
        return ReplacingHandle(handle) if Path(path) == output else handle

    monkeypatch.setattr(
        "workflows.integration_outputs._open_regular_readonly",
        open_with_replacement,
    )

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert replaced is True
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    assert manifest["unverifiable_outputs"][0]["reason"] == "file_digest_failed"
    snapshot_root = run_directory / ".verified-outputs"
    assert not list(snapshot_root.glob(".snapshot-*"))
    assert not [item for item in snapshot_root.iterdir() if not item.name.startswith(".")]


def test_output_snapshot_rejects_ancestor_symlink_replacement(
    analysis_workspace, tmp_path, monkeypatch
):
    asset, revision = _asset("snapshot-ancestor", "SnapshotAncestor")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-ancestor"
    output_directory = run_directory / "lane"
    output_directory.mkdir(parents=True)
    output = output_directory / "result.txt"
    output.write_text("trusted\n", encoding="utf-8")
    outside_directory = tmp_path / "outside-output"
    outside_directory.mkdir()
    (outside_directory / output.name).write_text("secret\n", encoding="utf-8")
    moved_directory = run_directory / "lane-original"
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotAncestor",
        sample_id="ANCESTOR",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotAncestor.file": str(output)}},
    )
    original_open = analysis_runs_module._open_regular_readonly
    replaced = False

    def open_after_ancestor_replacement(path, **kwargs):
        nonlocal replaced
        if Path(path) == output and not replaced:
            output_directory.rename(moved_directory)
            output_directory.symlink_to(outside_directory, target_is_directory=True)
            replaced = True
        return original_open(path, **kwargs)

    monkeypatch.setattr(
        "workflows.integration_outputs._open_regular_readonly",
        open_after_ancestor_replacement,
    )

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert replaced is True
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    assert manifest["unverifiable_outputs"] == [
        {
            "key": "SnapshotAncestor.file",
            "reason": "file_digest_failed",
        }
    ]
    snapshot_root = run_directory / ".verified-outputs"
    assert list(snapshot_root.iterdir()) == []


def test_output_traversal_budget_counts_null_array_items(
    analysis_workspace, settings
):
    asset, revision = _asset("snapshot-null-budget", "SnapshotNullBudget")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-null-budget"
    run_directory.mkdir(parents=True)
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotNullBudget",
        sample_id="NULLS",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotNullBudget.values": [None, None, None]}},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS = 2

    manifest, output_status, _ = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert manifest["unverifiable_outputs"] == [
        {
            "key": "SnapshotNullBudget.values[1]",
            "reason": "output_snapshot_item_limit_exceeded",
        }
    ]


def test_contracted_output_container_shapes_fail_closed(analysis_workspace):
    asset, revision = _asset("output-shapes", "OutputShapes")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "output-shapes"
    run_directory.mkdir(parents=True)
    contract = {
        "key": "OutputShapes.value",
        "name": "value",
        "label": "Value",
        "semantic_type": "core.value.string",
        "wdl_type": "Array[String]",
        "required": True,
    }
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="OutputShapes",
        sample_id="SHAPES",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        request_payload={"integration_output_contract": [contract]},
        outputs={"outputs": {"OutputShapes.value": []}},
    )

    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert output_status == AnalysisRun.OutputStatus.COMPLETE
    assert error is None
    assert manifest["items"][0]["value"] == []

    run.outputs = {"outputs": {"OutputShapes.value": "not-an-array"}}
    manifest, output_status, _ = build_output_manifest(run, run.outputs)
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert manifest["unverifiable_outputs"][0]["reason"] == "output_type_invalid"

    contract["wdl_type"] = "Pair[String,String]"
    run.request_payload = {"integration_output_contract": [contract]}
    run.outputs = {
        "outputs": {"OutputShapes.value": {"left": None, "right": "ok"}}
    }
    manifest, output_status, _ = build_output_manifest(run, run.outputs)
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert manifest["unverifiable_outputs"][0]["key"] == "OutputShapes.value.left"


def test_uncontracted_output_keys_are_bounded(analysis_workspace, settings):
    asset, revision = _asset("output-extra-keys", "OutputExtraKeys")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "output-extra-keys"
    run_directory.mkdir(parents=True)
    contract = {
        "key": "OutputExtraKeys.expected",
        "name": "expected",
        "semantic_type": "core.value.string",
        "wdl_type": "String",
        "required": True,
    }
    values = {"OutputExtraKeys.expected": "ok"}
    values.update({f"OutputExtraKeys.extra{index}": index for index in range(10)})
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="OutputExtraKeys",
        sample_id="EXTRA",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        request_payload={"integration_output_contract": [contract]},
        outputs={"outputs": values},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS = 2

    manifest, output_status, _ = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert manifest["uncontracted_output_key_count"] == 10
    assert len(manifest["uncontracted_output_keys"]) == 2
    assert manifest["uncontracted_output_keys_truncated"] is True
    assert manifest["unverifiable_outputs"][-1]["reason"] == (
        "output_manifest_uncontracted_key_limit_exceeded"
    )


def test_missing_output_contract_is_bounded(analysis_workspace, settings):
    asset, revision = _asset("missing-contract-budget", "MissingContractBudget")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "missing-contract-budget"
    run_directory.mkdir(parents=True)
    contract = [
        {
            "key": f"MissingContractBudget.result{index}",
            "name": f"result{index}",
            "semantic_type": "core.value.string",
            "wdl_type": "String",
            "required": True,
        }
        for index in range(10)
    ]
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="MissingContractBudget",
        sample_id="MISSING-CONTRACT-BUDGET",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        request_payload={"integration_output_contract": contract},
        outputs={"outputs": {}},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS = 2

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "REQUIRED_OUTPUT_MISSING"
    assert len(manifest["missing_required"]) == 2
    assert manifest["missing_required_truncated"] is True
    assert manifest["unverifiable_outputs"][-1]["reason"] == (
        "output_snapshot_item_limit_exceeded"
    )


def test_directory_output_cannot_overlap_snapshot_store(analysis_workspace):
    asset, revision = _asset("snapshot-directory-conflict", "SnapshotConflict")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-directory-conflict"
    snapshot_root = run_directory / ".verified-outputs"
    snapshot_root.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("result\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotConflict",
        sample_id="CONFLICT",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        request_payload={
            "integration_output_contract": [
                {
                    "key": "SnapshotConflict.directory",
                    "wdl_type": "Directory",
                    "required": True,
                },
                {
                    "key": "SnapshotConflict.file",
                    "wdl_type": "File",
                    "required": True,
                },
            ]
        },
        outputs={
            "outputs": {
                "SnapshotConflict.directory": str(snapshot_root),
                "SnapshotConflict.file": str(output),
            }
        },
    )

    manifest, output_status, _ = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert manifest["unverifiable_outputs"][0]["reason"] == (
        "output_directory_conflicts_with_snapshot_store"
    )


def test_output_snapshot_checkpoint_aborts_and_removes_temporary_file(
    analysis_workspace, settings
):
    asset, revision = _asset("snapshot-cancel", "SnapshotCancel")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-cancel"
    run_directory.mkdir(parents=True)
    output = run_directory / "large.bin"
    output.write_bytes(b"x" * (2 * 1024 * 1024))
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotCancel",
        sample_id="CANCEL",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotCancel.file": str(output)}},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES = 0
    checkpoints = 0

    def cancel_during_copy():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 3:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        build_output_manifest(
            run,
            run.outputs,
            checkpoint=cancel_during_copy,
        )

    snapshot_root = run_directory / ".verified-outputs"
    assert not list(snapshot_root.glob(".snapshot-*"))


def test_output_snapshot_checkpoint_before_publish_leaves_no_target(
    analysis_workspace, settings
):
    asset, revision = _asset("snapshot-prepublish-cancel", "SnapshotPrepublishCancel")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-prepublish-cancel"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.bin"
    output.write_bytes(b"verified output")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotPrepublishCancel",
        sample_id="PREPUBLISH-CANCEL",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={
            "outputs": {"SnapshotPrepublishCancel.result": str(output)}
        },
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES = 0
    checkpoints = 0

    def cancel_during_snapshot_rehash():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 5:
            raise RuntimeError("lease lost before publish")

    with pytest.raises(RuntimeError, match="lease lost before publish"):
        build_output_manifest(
            run,
            run.outputs,
            checkpoint=cancel_during_snapshot_rehash,
        )

    snapshot_root = run_directory / ".verified-outputs"
    assert list(snapshot_root.iterdir()) == []


def test_output_snapshot_directory_fsync_failure_removes_published_target(
    analysis_workspace, settings, monkeypatch
):
    asset, revision = _asset("snapshot-fsync-failure", "SnapshotFsyncFailure")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-fsync-failure"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.bin"
    output.write_bytes(b"verified output")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotFsyncFailure",
        sample_id="FSYNC-FAILURE",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotFsyncFailure.result": str(output)}},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES = 0
    original_fsync = os.fsync

    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed")
        return original_fsync(descriptor)

    monkeypatch.setattr("workflows.integration_outputs.os.fsync", fail_directory_fsync)

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    assert manifest["unverifiable_outputs"][0]["reason"] == "file_digest_failed"
    snapshot_root = run_directory / ".verified-outputs"
    assert not [path for path in snapshot_root.iterdir() if not path.name.startswith(".")]


def test_output_snapshot_accepts_hardlink_ctime_change(
    analysis_workspace, settings, monkeypatch
):
    asset, revision = _asset("snapshot-link-ctime", "SnapshotLinkCtime")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "snapshot-link-ctime"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.bin"
    output.write_bytes(b"verified output")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SnapshotLinkCtime",
        sample_id="LINK-CTIME",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"SnapshotLinkCtime.result": str(output)}},
    )
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES = 0
    original_link = os.link
    ctime_changed = False

    def link_with_distinct_ctime(source, target, **kwargs):
        nonlocal ctime_changed
        before_ctime = os.stat(
            source,
            dir_fd=kwargs.get("src_dir_fd"),
            follow_symlinks=False,
        ).st_ctime_ns
        original_link(source, target, **kwargs)
        for _ in range(100):
            os.chmod(target, 0o444, dir_fd=kwargs.get("dst_dir_fd"))
            if (
                os.stat(
                    target,
                    dir_fd=kwargs.get("dst_dir_fd"),
                    follow_symlinks=False,
                ).st_ctime_ns
                != before_ctime
            ):
                ctime_changed = True
                return
            time.sleep(0.001)
        raise AssertionError("hardlink ctime did not change")

    monkeypatch.setattr("workflows.integration_outputs.os.link", link_with_distinct_ctime)

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert ctime_changed is True
    assert error is None
    assert output_status == AnalysisRun.OutputStatus.COMPLETE
    assert output_manifest_is_current(manifest)


def test_output_directory_snapshot_uses_one_global_entry_budget(
    analysis_workspace, settings
):
    asset, revision = _asset("directory-budget", "DirectoryBudget")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "directory-budget"
    output_directory = run_directory / "result"
    output_directory.mkdir(parents=True)
    (output_directory / "one.txt").write_text("one\n", encoding="utf-8")
    (output_directory / "two.txt").write_text("two\n", encoding="utf-8")
    settings.ANALYSIS_OUTPUT_SNAPSHOT_MAX_DIRECTORY_ENTRIES = 3
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="DirectoryBudget",
        sample_id="DIRECTORY-BUDGET",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        request_payload={
            "integration_output_contract": [
                {
                    "key": "DirectoryBudget.result",
                    "wdl_type": "Directory",
                    "required": True,
                }
            ]
        },
        outputs={
            "outputs": {"DirectoryBudget.result": str(output_directory)}
        },
    )

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    assert manifest["unverifiable_outputs"] == [
        {
            "key": "DirectoryBudget.result",
            "reason": "output_snapshot_directory_entry_limit_exceeded",
        }
    ]


def test_output_directory_snapshot_reuses_identical_source(
    analysis_workspace,
):
    asset, revision = _asset("directory-cache", "DirectoryCache")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "directory-cache"
    output_directory = run_directory / "result"
    output_directory.mkdir(parents=True)
    (output_directory / "result.txt").write_text("ok\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="DirectoryCache",
        sample_id="DIRECTORY-CACHE",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        request_payload={
            "integration_output_contract": [
                {"key": key, "wdl_type": "Directory", "required": True}
                for key in ("DirectoryCache.first", "DirectoryCache.second")
            ]
        },
        outputs={
            "outputs": {
                "DirectoryCache.first": str(output_directory),
                "DirectoryCache.second": str(output_directory),
            }
        },
    )

    with patch(
        "workflows.integration_outputs._directory_manifest",
        wraps=_directory_manifest,
    ) as snapshot:
        manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert error is None
    assert output_status == AnalysisRun.OutputStatus.COMPLETE
    assert len(manifest["items"]) == 2
    assert snapshot.call_count == 2


def test_directory_manifest_closes_child_descriptor_when_checkpoint_aborts(
    tmp_path: Path,
):
    root = tmp_path / "descriptor-cleanup"
    (root / "child").mkdir(parents=True)
    opened: list[int] = []
    closed: list[int] = []
    original_open = os.open
    original_close = os.close

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracked_close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    def abort_after_child_open():
        if len(opened) >= 2:
            raise RuntimeError("lease lost")

    with (
        patch("workflows.directory_identity.os.open", side_effect=tracked_open),
        patch("workflows.directory_identity.os.close", side_effect=tracked_close),
        pytest.raises(RuntimeError, match="lease lost"),
    ):
        _directory_manifest(root, checkpoint=abort_after_child_open)

    assert opened
    assert set(opened) <= set(closed)


def test_browser_output_download_refuses_legacy_file_without_manifest(
    client, analysis_workspace
):
    asset, revision = _asset("legacy-output", "LegacyOutput")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "legacy-run"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("legacy\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LegacyOutput",
        sample_id="LEGACY",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"LegacyOutput.result": str(output)}},
    )

    response = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "LegacyOutput.result"},
    )

    assert response.status_code == 409
    assert response.data["error"]["code"] == "ANALYSIS_OUTPUT_UNVERIFIED"

    dry_run = io.StringIO()
    call_command(
        "backfill_analysis_output_manifests",
        dry_run=True,
        stdout=dry_run,
    )
    run.refresh_from_db()
    assert run.output_manifest == {}
    assert "待补建 1 条" in dry_run.getvalue()

    call_command(
        "backfill_analysis_output_manifests",
        actor="pytest",
        stdout=io.StringIO(),
    )
    run.refresh_from_db()
    assert run.output_manifest["schema_version"] == 1
    assert run.output_manifest["provenance"]["kind"] == "historical_backfill"
    assert run.output_manifest["provenance"]["source"] == "management-command:pytest"
    assert run.output_manifest["provenance"]["baselined_at"]
    assert run.events.filter(kind="output_manifest_backfill").count() == 1

    downloaded = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "LegacyOutput.result"},
    )
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == b"legacy\n"

    call_command(
        "backfill_analysis_output_manifests",
        actor="pytest-rerun",
        stdout=io.StringIO(),
    )
    assert run.events.filter(kind="output_manifest_backfill").count() == 1


def test_browser_output_download_treats_schema_v1_without_integrity_as_unverified(
    client, analysis_workspace
):
    asset, revision = _asset("legacy-v1-output", "LegacyV1Output")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "legacy-v1-run"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("legacy\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LegacyV1Output",
        sample_id="LEGACY-V1",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"LegacyV1Output.result": str(output)}},
        output_manifest={
            "schema_version": 1,
            "items": [
                {
                    "key": "LegacyV1Output.result",
                    "kind": "file",
                    "path": str(output),
                    "sha256": _sha256(output),
                }
            ],
        },
    )

    detail = client.get(f"/api/v1/analysis-runs/{run.id}")
    assert detail.status_code == 200
    assert "download_url" not in detail.data["outputs"][0]
    download = client.get(
        f"/api/v1/analysis-runs/{run.id}/outputs",
        {"key": "LegacyV1Output.result"},
    )

    assert download.status_code == 409
    assert download.data["error"]["code"] == "ANALYSIS_OUTPUT_UNVERIFIED"


def test_backfill_preserves_legacy_file_evidence_and_created_at(
    analysis_workspace,
):
    asset, revision = _asset("legacy-evidence", "LegacyEvidence")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "legacy-evidence"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("original\n", encoding="utf-8")
    created_at = "2025-01-02T03:04:05+00:00"
    legacy_manifest = {
        "schema_version": 1,
        "created_at": created_at,
        "items": [
            {
                "key": "LegacyEvidence.result",
                "kind": "file",
                "path": str(output),
                "sha256": _sha256(output),
            }
        ],
    }
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LegacyEvidence",
        sample_id="EVIDENCE",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"LegacyEvidence.result": str(output)}},
        output_manifest=legacy_manifest,
        output_status=AnalysisRun.OutputStatus.INCOMPLETE,
        error="输出清单不完整。",
        error_code="OUTPUT_INTEGRITY_UNVERIFIABLE",
        error_category="application",
        error_details={"unverifiable": ["LegacyEvidence.result"]},
    )

    assert backfill_output_manifest(run, source="pytest") is True
    run.refresh_from_db()

    assert run.output_manifest["integrity_version"] == 2
    assert run.output_manifest["created_at"] == created_at
    assert run.output_manifest["items"][0]["sha256"] == legacy_manifest["items"][0]["sha256"]
    assert run.output_status == AnalysisRun.OutputStatus.COMPLETE
    assert run.error == ""
    assert run.error_code == ""
    assert run.error_category == ""
    assert run.error_retryable is False
    assert run.error_details == {}
    provenance = run.output_manifest["provenance"]
    assert provenance["kind"] == "completion_manifest_upgrade"
    assert provenance["previous_manifest_digest"].startswith("sha256:")
    assert provenance["baselined_directory_keys"] == []


def test_backfill_rejects_unknown_legacy_manifest_item(analysis_workspace):
    asset, revision = _asset("legacy-unknown-item", "LegacyUnknownItem")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "legacy-unknown-item"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("original\n", encoding="utf-8")
    legacy_manifest = {
        "schema_version": 1,
        "items": [{"key": "LegacyUnknownItem.result", "kind": "mystery"}],
    }
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LegacyUnknownItem",
        sample_id="UNKNOWN-ITEM",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"LegacyUnknownItem.result": str(output)}},
        output_manifest=legacy_manifest,
    )

    with pytest.raises(ValueError, match="未知条目类型"):
        backfill_output_manifest(run, source="pytest")
    run.refresh_from_db()

    assert run.output_manifest == legacy_manifest


def test_backfill_repairs_status_for_current_manifest(analysis_workspace):
    asset, revision = _asset("current-status", "CurrentStatus")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "current-status"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("original\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="CurrentStatus",
        sample_id="CURRENT-STATUS",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"CurrentStatus.result": str(output)}},
    )
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert error is None
    assert output_status == AnalysisRun.OutputStatus.COMPLETE
    run.output_manifest = manifest
    run.output_status = AnalysisRun.OutputStatus.INCOMPLETE
    run.error = "输出清单不完整。"
    run.error_code = "REQUIRED_OUTPUT_MISSING"
    run.error_category = "application"
    run.error_retryable = False
    run.error_details = {"missing": ["CurrentStatus.result"]}
    run.save(
        update_fields=[
            "output_manifest",
            "output_status",
            "error",
            "error_code",
            "error_category",
            "error_retryable",
            "error_details",
            "updated_at",
        ]
    )

    assert backfill_output_manifest(run, source="pytest") is True
    run.refresh_from_db()

    assert run.output_status == AnalysisRun.OutputStatus.COMPLETE
    assert run.error == ""
    assert run.error_code == ""
    assert run.error_category == ""
    assert run.error_retryable is False
    assert run.error_details == {}
    assert run.events.filter(
        kind="output_manifest_backfill",
        details__repair="output_status",
    ).exists()


def test_backfill_command_reports_resumable_failure_cursor(analysis_workspace):
    asset, revision = _asset("backfill-cursor", "BackfillCursor")
    _, _, runs, _ = analysis_workspace
    first_id = uuid.UUID(int=1)
    second_id = uuid.UUID(int=2)
    first = AnalysisRun.objects.create(
        id=first_id,
        asset=asset,
        revision=revision,
        workflow_name="BackfillCursor",
        sample_id="CURSOR-FAIL",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(runs / "missing"),
        outputs={"outputs": {}},
    )
    run_directory = runs / "cursor-success"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("ok\n", encoding="utf-8")
    second = AnalysisRun.objects.create(
        id=second_id,
        asset=asset,
        revision=revision,
        workflow_name="BackfillCursor",
        sample_id="CURSOR-SUCCESS",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"BackfillCursor.result": str(output)}},
    )

    with pytest.raises(CommandError, match=f"最后 ID: {first.id}"):
        call_command(
            "backfill_analysis_output_manifests",
            limit=1,
            actor="pytest",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
    second.refresh_from_db()
    assert second.output_manifest == {}

    call_command(
        "backfill_analysis_output_manifests",
        after_id=str(first.id),
        limit=1,
        actor="pytest",
        stdout=io.StringIO(),
    )
    second.refresh_from_db()
    assert output_manifest_is_current(second.output_manifest)


def test_backfill_rejects_tampered_legacy_file_without_overwriting_evidence(
    analysis_workspace,
):
    asset, revision = _asset("legacy-tamper", "LegacyTamper")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "legacy-tamper"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("original\n", encoding="utf-8")
    legacy_manifest = {
        "schema_version": 1,
        "items": [
            {
                "key": "LegacyTamper.result",
                "kind": "file",
                "path": str(output),
                "sha256": _sha256(output),
            }
        ],
    }
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LegacyTamper",
        sample_id="TAMPER",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"LegacyTamper.result": str(output)}},
        output_manifest=legacy_manifest,
        output_status=AnalysisRun.OutputStatus.COMPLETE,
    )
    output.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="原完整性证据不一致"):
        backfill_output_manifest(run, source="pytest")
    run.refresh_from_db()

    assert run.output_manifest == legacy_manifest
    assert run.events.filter(kind="output_manifest_backfill").count() == 0


def test_backfill_records_legacy_directory_without_digest_as_baseline(
    analysis_workspace,
):
    asset, revision = _asset("legacy-directory", "LegacyDirectory")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "legacy-directory"
    output_directory = run_directory / "result"
    output_directory.mkdir(parents=True)
    (output_directory / "summary.txt").write_text("ok\n", encoding="utf-8")
    legacy_manifest = {
        "schema_version": 1,
        "items": [
            {
                "key": "LegacyDirectory.result",
                "kind": "directory",
                "path": str(output_directory),
                "identity": {},
            }
        ],
    }
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LegacyDirectory",
        sample_id="DIRECTORY",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"LegacyDirectory.result": str(output_directory)}},
        output_manifest=legacy_manifest,
    )

    assert backfill_output_manifest(run, source="pytest") is True
    run.refresh_from_db()

    assert run.output_manifest["items"][0]["digest"].startswith("sha256:")
    assert run.output_manifest["provenance"]["baselined_directory_keys"] == [
        "LegacyDirectory.result"
    ]


def test_backfill_does_not_overwrite_concurrently_changed_manifest(
    analysis_workspace,
):
    asset, revision = _asset("legacy-concurrent", "LegacyConcurrent")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "legacy-concurrent"
    run_directory.mkdir(parents=True)
    output = run_directory / "result.txt"
    output.write_text("original\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LegacyConcurrent",
        sample_id="CONCURRENT",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"LegacyConcurrent.result": str(output)}},
    )
    original_builder = build_output_manifest
    concurrent_manifest = {"schema_version": 1, "items": [], "changed": True}

    def build_after_concurrent_change(*args, **kwargs):
        result = original_builder(*args, **kwargs)
        AnalysisRun.objects.filter(pk=run.pk).update(
            output_manifest=concurrent_manifest
        )
        return result

    with patch(
        "workflows.integration_outputs.build_output_manifest",
        side_effect=build_after_concurrent_change,
    ):
        assert backfill_output_manifest(run, source="pytest") is False
    run.refresh_from_db()

    assert run.output_manifest == concurrent_manifest
    assert run.events.filter(kind="output_manifest_backfill").count() == 0


def test_backfill_rejects_missing_uncontracted_output_path(analysis_workspace):
    asset, revision = _asset("legacy-missing-file", "LegacyMissingFile")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "legacy-missing-file"
    run_directory.mkdir(parents=True)
    missing = run_directory / "result.txt"
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="LegacyMissingFile",
        sample_id="MISSING-FILE",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"LegacyMissingFile.result": str(missing)}},
    )

    with pytest.raises(ValueError, match="无法生成完整"):
        backfill_output_manifest(run, source="pytest")
    run.refresh_from_db()

    assert run.output_manifest == {}


def test_output_manifest_current_check_rejects_malformed_items():
    assert output_manifest_is_current(
        {
            "schema_version": 1,
            "integrity_version": 2,
            "items": None,
            "missing_required": [],
            "unverifiable_outputs": [],
        }
    ) is False


def test_resource_verification_timeout_has_stable_retryable_failure_metadata():
    assert _failure_metadata("目录快照超过时间上限：database") == {
        "code": "ANALYSIS_RESOURCE_VERIFICATION_TIMEOUT",
        "category": "infrastructure",
        "retryable": True,
    }


def test_uncontracted_unverifiable_directory_marks_output_incomplete(
    client, analysis_workspace,
):
    asset, revision = _asset("unverifiable-output", "UnverifiableOutput")
    _, _, runs, _ = analysis_workspace
    run_directory = runs / "unverifiable-run"
    output_directory = run_directory / "result"
    output_directory.mkdir(parents=True)
    (output_directory / "link").symlink_to(run_directory)
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="UnverifiableOutput",
        sample_id="UNVERIFIABLE",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"UnverifiableOutput.result": str(output_directory)}},
    )

    manifest, output_status, error = build_output_manifest(run, run.outputs)

    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    assert manifest["unverifiable_outputs"] == [
        {
            "key": "UnverifiableOutput.result",
            "reason": "directory_snapshot_failed",
        }
    ]
    assert manifest["items"][0]["kind"] == "unverifiable"
    run.output_manifest = manifest
    run.output_status = output_status
    run.error = "输出清单不完整。"
    run.error_code = error["code"]
    run.error_category = error["category"]
    run.error_retryable = error["retryable"]
    run.error_details = error["details"]
    run.save(
        update_fields=[
            "output_manifest",
            "output_status",
            "error",
            "error_code",
            "error_category",
            "error_retryable",
            "error_details",
            "updated_at",
        ]
    )

    detail = client.get(f"/api/v1/analysis-runs/{run.id}")

    assert detail.status_code == 200
    assert detail.data["status"] == AnalysisRun.Status.SUCCEEDED
    assert detail.data["output_status"] == AnalysisRun.OutputStatus.INCOMPLETE
    assert detail.data["error_code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    assert detail.data["error_details"]["unverifiable"][0]["key"] == (
        "UnverifiableOutput.result"
    )


def test_output_manifest_backfill_fails_when_historical_directory_is_missing(
    analysis_workspace,
):
    asset, revision = _asset("missing-legacy-output", "MissingLegacyOutput")
    _, _, runs, _ = analysis_workspace
    AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="MissingLegacyOutput",
        sample_id="MISSING",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(runs / "missing-run"),
        outputs={"outputs": {}},
    )

    with pytest.raises(CommandError, match="成功 0，失败 1"):
        call_command(
            "backfill_analysis_output_manifests",
            actor="pytest",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


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
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert error is None
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["output_manifest", "output_status", "updated_at"])

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

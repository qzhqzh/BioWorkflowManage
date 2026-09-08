from __future__ import annotations

import os
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from workflows.analysis_runtime import _verify_run_resource_manifests
from workflows.models import RawdataBatchReadiness, RawdataScan
from workflows.rawdata_readiness import rawdata_readiness, verify_readiness_snapshot

from test_integration_api import _submission, _token_client, _workflow_version
from test_integration_api import integration_workspace as integration_workspace
from test_rawdata_catalog import _finish_index

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("auth_disabled")]


@pytest.fixture
def batch(tmp_path, settings):
    root = tmp_path / "rawdata"
    directory = root / "batch"
    directory.mkdir(parents=True)
    for mate in (1, 2):
        (directory / f"S1_R{mate}.fastq.gz").write_bytes(b"reads")
    settings.ANALYSIS_RAWDATA_ROOT = root
    return root, directory, [f"batch/S1_R{mate}.fastq.gz" for mate in (1, 2)]


def test_marker_waiting_then_indexed_ready(batch, settings):
    root, directory, files = batch
    _finish_index(settings, root)
    assert rawdata_readiness(files)["status"] == "waiting"
    (directory / "_READY.done").touch()
    assert rawdata_readiness(files)["status"] == "unknown"
    _finish_index(settings, root)
    result = rawdata_readiness(files)
    assert result["submission_allowed"] is True
    assert result["snapshot"]["files"] == files
    assert RawdataBatchReadiness.objects.count() == 1


@pytest.mark.parametrize(
    "kind", ["directory", "symlink", "empty", "missing_mate", "future"]
)
def test_marker_and_batch_invalid(batch, settings, kind):
    root, directory, files = batch
    marker = directory / "_READY.done"
    marker.touch()
    if kind == "directory":
        marker.unlink()
        marker.mkdir()
    elif kind == "symlink":
        marker.unlink()
        marker.symlink_to(directory / "S1_R1.fastq.gz")
    elif kind == "empty":
        (directory / "S1_R1.fastq.gz").write_bytes(b"")
    elif kind == "missing_mate":
        (directory / "OTHER_R1.fastq.gz").write_bytes(b"reads")
        marker.touch()
    else:
        path = directory / "S1_R1.fastq.gz"
        os.utime(path, ns=(marker.stat().st_mtime_ns + 1000000,) * 2)
    _finish_index(settings, root)
    assert rawdata_readiness(files)["submission_allowed"] is False
    assert RawdataBatchReadiness.objects.count() == 0


@pytest.mark.parametrize("kind", ["replace", "add", "remove", "overwrite", "empty"])
def test_same_marker_change_latched_and_new_generation_required(batch, settings, kind):
    root, directory, files = batch
    marker = directory / "_READY.done"
    marker.touch()
    _finish_index(settings, root)
    snapshot = rawdata_readiness(files)["snapshot"]
    path = directory / "S1_R1.fastq.gz"
    original = path.stat()
    if kind == "replace":
        path.unlink()
        path.write_bytes(b"reads")
    elif kind == "add":
        (directory / "OTHER_R1.fastq.gz").write_bytes(b"reads")
    elif kind == "remove":
        path.unlink()
    elif kind == "overwrite":
        path.write_bytes(b"other")
    else:
        path.write_bytes(b"")
    if path.exists():
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert rawdata_readiness(files)["status"] == "changed"
    # 即使恢复内容和 mtime，同一 marker 也不能解除锁存。
    path.write_bytes(b"reads")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    extra = directory / "OTHER_R1.fastq.gz"
    if extra.exists():
        extra.unlink()
    _finish_index(settings, root)
    assert rawdata_readiness(files)["status"] == "changed"
    marker.unlink()
    marker.touch()
    _finish_index(settings, root)
    assert rawdata_readiness(files)["status"] == "ready"
    with pytest.raises(ValueError, match="SNAPSHOT_CHANGED"):
        verify_readiness_snapshot(
            snapshot, {"files": [{"relative_path": item} for item in files]}
        )


def test_snapshot_matches_inputs_and_worker_rechecks(batch, settings, monkeypatch):
    root, directory, files = batch
    (directory / "_READY.done").touch()
    _finish_index(settings, root)
    snapshot = rawdata_readiness(files)["snapshot"]
    manifest = {"files": [{"relative_path": item} for item in files]}
    assert verify_readiness_snapshot(snapshot, manifest) == snapshot
    with pytest.raises(ValueError, match="INPUT_MISMATCH"):
        verify_readiness_snapshot(snapshot, {"files": [{"relative_path": files[0]}]})
    monkeypatch.setattr(
        "workflows.analysis_runtime._verify_manifest_files", lambda *a, **kw: None
    )
    run = SimpleNamespace(
        request_payload={
            "rawdata_readiness": snapshot,
            "input_resource_manifest": manifest,
        }
    )
    _verify_run_resource_manifests(run)
    (directory / "_READY.done").unlink()
    with pytest.raises(ValueError, match="NOT_READY"):
        _verify_run_resource_manifests(run)
    # 历史任务不带新协议字段，保留原有执行规则。
    _verify_run_resource_manifests(
        SimpleNamespace(request_payload={"input_resource_manifest": manifest})
    )


def test_index_stale_submission_blocked_but_snapshot_worker_allowed(batch, settings):
    root, directory, files = batch
    (directory / "_READY.done").touch()
    scan = _finish_index(settings, root)
    snapshot = rawdata_readiness(files)["snapshot"]
    RawdataScan.objects.filter(pk=scan.pk).update(
        finished_at=timezone.now() - timedelta(days=1)
    )
    assert rawdata_readiness(files)["status"] == "unknown"
    assert (
        verify_readiness_snapshot(
            snapshot,
            {"files": [{"relative_path": item} for item in files]},
            require_fresh_index=False,
        )
        == snapshot
    )


def test_worker_uses_frozen_index_key_with_different_mount(batch, settings, monkeypatch):
    root, directory, files = batch
    (directory / "_READY.done").touch()
    _finish_index(settings, root)
    snapshot = rawdata_readiness(files)["snapshot"]
    manifest = {"files": [{"relative_path": item} for item in files]}
    # 文件仍是同一共享数据；仅 worker 的挂载路径所产生的索引键不同。
    monkeypatch.setattr("workflows.rawdata_index.rawdata_root_key", lambda: "sha256:" + "0" * 64)
    assert verify_readiness_snapshot(snapshot, manifest, require_fresh_index=False) == snapshot
    with pytest.raises(ValueError, match="SNAPSHOT_INVALID"):
        verify_readiness_snapshot(snapshot, manifest)
    (directory / "_READY.done").unlink()
    with pytest.raises(ValueError, match="NOT_READY"):
        verify_readiness_snapshot(snapshot, manifest, require_fresh_index=False)


def test_readiness_api_batch_and_legacy_catalog(batch, settings):
    root, directory, files = batch
    _finish_index(settings, root)
    _, _, _, client = _token_client(scopes=["analysis:read"])
    old = client.get("/api/v1/integration/rawdata-datasets")
    assert old.status_code == 200 and old.data["count"] == 1
    assert "readiness" not in old.data["results"][0]
    extended = client.get("/api/v1/integration/rawdata-datasets?include_unready=true")
    assert extended.data["results"][0]["readiness"]["status"] == "waiting"
    response = client.post(
        "/api/v1/integration/rawdata-datasets/readiness",
        {"datasets": [{"key": "one", "files": files}, {"key": "two", "files": []}]},
        format="json",
    )
    assert response.status_code == 200
    assert [item["status"] for item in response.data["results"]] == [
        "waiting",
        "unbound",
    ]
    assert (
        client.post(
            "/api/v1/integration/rawdata-datasets/readiness",
            {"datasets": [{"key": "one", "files": files}] * 2},
            format="json",
        ).status_code
        == 400
    )


def test_batch_request_reuses_directory_observation_only_within_request(
    batch, settings
):
    from workflows.rawdata_readiness import _observe

    root, directory, files = batch
    (directory / "_READY.done").touch()
    _finish_index(settings, root)
    _, _, _, client = _token_client(scopes=["analysis:read"])
    body = {"datasets": [{"key": str(i), "files": files} for i in range(100)]}
    with patch("workflows.rawdata_readiness._observe", wraps=_observe) as observe:
        for expected_calls in (1, 2):
            response = client.post(
                "/api/v1/integration/rawdata-datasets/readiness", body, format="json"
            )
            assert all(item["submission_allowed"] for item in response.data["results"])
            assert observe.call_count == expected_calls


@pytest.mark.django_db(transaction=True)
def test_additive_migration_on_isolated_database():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    assert "memory" in str(connection.settings_dict["NAME"]) or "test" in str(
        connection.settings_dict["NAME"]
    )
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([("workflows", "0033_analysisrun_execution_engine_and_more")])
        tables = connection.introspection.table_names()
        assert "workflows_rawdatabatchreadiness" not in tables
        assert "workflows_analysisrun" in tables
        executor = MigrationExecutor(connection)
        executor.migrate([("workflows", "0034_rawdata_batch_readiness")])
        assert (
            "workflows_rawdatabatchreadiness" in connection.introspection.table_names()
        )
    finally:
        MigrationExecutor(connection).migrate(
            [("workflows", "0034_rawdata_batch_readiness")]
        )


@pytest.mark.parametrize(
    "files",
    [None, "bad", ["../outside"], ["/absolute"], ["a", "a"], ["./a"], ["a"] * 65],
)
def test_invalid_paths_fail_closed(files):
    assert rawdata_readiness(files)["status"] == "issue"


def test_preserved_mtime_copy_after_marker_cannot_establish_baseline(batch, settings):
    root, directory, files = batch
    path = directory / "S1_R1.fastq.gz"
    before = path.stat()
    (directory / "_READY.done").touch()
    # 避免文件系统时间戳精度使两个不同操作落在同一 tick。
    time.sleep(0.02)
    path.write_bytes(b"other")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    _finish_index(settings, root)
    assert rawdata_readiness(files)["status"] == "issue"
    assert RawdataBatchReadiness.objects.count() == 0


def test_preflight_acceptance_freezes_snapshot_and_rejects_change(
    integration_workspace, settings
):
    from workflows.models import AnalysisRun

    root, _, _ = integration_workspace
    (root / "_READY.done").touch()
    _finish_index(settings, root)
    files = ["S001_R1.fastq.gz", "S001_R2.fastq.gz"]
    snapshot = rawdata_readiness(files)["snapshot"]
    assert snapshot is not None
    _, _, _, client = _token_client()
    body = _submission(_workflow_version())
    body["rawdata_readiness"] = snapshot
    preflight = client.post(
        "/api/v1/integration/analysis-runs/preflight", body, format="json"
    )
    assert preflight.status_code == 200, preflight.data
    response = client.post(
        "/api/v1/integration/analysis-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="ready-one",
    )
    assert response.status_code == 201, response.data
    run = AnalysisRun.objects.get()
    assert run.request_payload["rawdata_readiness"] == snapshot
    (root / "_READY.done").unlink()
    (root / "_READY.done").touch()
    _finish_index(settings, root)
    blocked = client.post(
        "/api/v1/integration/analysis-runs/preflight", body, format="json"
    )
    assert blocked.status_code == 400, blocked.data
    assert blocked.data["error"]["code"] == "RAWDATA_NOT_READY"
    body["external_ref"]["external_run_id"] = "new-run"
    rejected = client.post(
        "/api/v1/integration/analysis-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="ready-two",
    )
    assert rejected.status_code == 400
    assert AnalysisRun.objects.count() == 1

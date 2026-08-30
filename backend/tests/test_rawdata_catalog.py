from __future__ import annotations

from pathlib import Path

import pytest

from workflows import rawdata_catalog
from workflows.models import RawdataDatasetIndex, RawdataScan
from workflows.rawdata_catalog import (
    advance_rawdata_scan,
    runnable_fastq_datasets,
    scan_rawdata_catalog,
)
from workflows.rawdata_index import (
    ensure_periodic_rawdata_scan,
    indexed_rawdata_catalog,
    queue_rawdata_scan,
    run_rawdata_scan_batch,
)


pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("auth_disabled")]


def _write(path: Path, content: bytes = b"reads\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _finish_index(settings, rawdata: Path, *, batch_entries: int = 1) -> RawdataScan:
    settings.ANALYSIS_RAWDATA_ROOT = rawdata
    settings.RAWDATA_SCAN_BATCH_ENTRIES = batch_entries
    settings.RAWDATA_SCAN_MAX_FILES = 100
    settings.RAWDATA_SCAN_MAX_ENTRIES = 100
    settings.RAWDATA_SCAN_MAX_DEPTH = 8
    settings.RAWDATA_SCAN_BATCH_SECONDS = 10
    settings.RAWDATA_SCAN_LEASE_SECONDS = 60
    scan, _ = queue_rawdata_scan(actor="tester", trigger="manual", root_value=rawdata)
    for _ in range(100):
        scan = run_rawdata_scan_batch(rawdata) or scan
        scan.refresh_from_db()
        if scan.status in {
            RawdataScan.Status.SUCCEEDED,
            RawdataScan.Status.LIMITED,
            RawdataScan.Status.FAILED,
        }:
            return scan
    raise AssertionError("rawdata index did not finish")


def test_scan_reports_ready_incomplete_empty_and_unrecognized_fastq(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "batch-a" / "SAMPLE01_L001_R1_001.fastq.gz")
    _write(rawdata / "batch-a" / "SAMPLE01_L001_R2_001.fastq.gz")
    _write(rawdata / "batch-a" / "SAMPLE02_R1.fastq.gz")
    _write(rawdata / "batch-b" / "SAMPLE03_R1.fastq.gz", b"")
    _write(rawdata / "batch-b" / "SAMPLE03_R2.fastq.gz")
    _write(rawdata / "batch-b" / "manual.fastq.gz")
    _write(rawdata / "batch-b" / "notes.txt")

    catalog = scan_rawdata_catalog(rawdata)

    assert catalog["root_status"] == "ready"
    assert catalog["summary"] == {
        "file_count": 6,
        "dataset_count": 3,
        "ready_dataset_count": 1,
        "issue_dataset_count": 2,
        "unrecognized_fastq_count": 1,
        "total_size": 30,
        "total_size_label": "30 B",
    }
    assert [item["name"] for item in catalog["datasets"]] == [
        "SAMPLE01",
        "SAMPLE02",
        "SAMPLE03",
    ]
    assert catalog["datasets"][0]["status"] == "ready"
    assert catalog["datasets"][1]["missing_mates"] == [2]
    assert catalog["datasets"][2]["issues"][0]["code"] == "RAWDATA_FILE_EMPTY"
    assert catalog["unrecognized_files"][0]["relative_path"] == (
        "batch-b/manual.fastq.gz"
    )
    assert [item["path"] for item in catalog["directories"]] == [
        "batch-a",
        "batch-b",
    ]


def test_runnable_datasets_exclude_incomplete_and_empty_pairs(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "ready_R1.fq.gz")
    _write(rawdata / "ready_R2.fq.gz")
    _write(rawdata / "missing_R1.fq.gz")
    _write(rawdata / "empty_R1.fq.gz", b"")
    _write(rawdata / "empty_R2.fq.gz")

    datasets = runnable_fastq_datasets(rawdata)

    assert [item["name"] for item in datasets] == ["ready"]
    assert [item["mate"] for item in datasets[0]["files"]] == [1, 2]


def test_scan_missing_root_returns_actionable_state(tmp_path):
    catalog = scan_rawdata_catalog(tmp_path / "not-mounted")

    assert catalog["root_status"] == "missing"
    assert catalog["datasets"] == []
    assert catalog["issues"] == [
        {
            "code": "RAWDATA_ROOT_MISSING",
            "message": "原始数据目录尚未挂载或不存在。",
        }
    ]


def test_scan_limit_is_reported_without_exposing_later_files(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "a_R1.fastq.gz")
    _write(rawdata / "a_R2.fastq.gz")
    _write(rawdata / "b_R1.fastq.gz")

    catalog = scan_rawdata_catalog(rawdata, max_files=2)

    assert catalog["scan_limited"] is True
    assert catalog["summary"]["ready_dataset_count"] == 0
    assert catalog["datasets"][0]["status"] == "scan_incomplete"
    assert catalog["datasets"][0]["missing_mates"] == []
    assert catalog["datasets"][0]["issues"] == [
        {
            "code": "RAWDATA_SCAN_INCOMPLETE",
            "message": "扫描未完成，暂时无法确认配对与重复文件。",
        }
    ]
    assert catalog["issues"][-1]["code"] == "RAWDATA_SCAN_LIMIT_REACHED"


def test_scan_entry_budget_limits_wide_or_deep_directories(tmp_path):
    rawdata = tmp_path / "rawdata"
    for index in range(4):
        (rawdata / f"empty-{index}").mkdir(parents=True)

    catalog = scan_rawdata_catalog(rawdata, max_entries=2)

    assert catalog["scan_limited"] is True
    assert catalog["scanned_entry_count"] == 2
    assert catalog["issues"][-1]["code"] == "RAWDATA_SCAN_LIMIT_REACHED"


def test_catalog_cache_reuses_recent_scan(monkeypatch, tmp_path):
    calls = 0
    expected = scan_rawdata_catalog(tmp_path / "missing")

    def fake_scan(root_value):
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(rawdata_catalog, "scan_rawdata_catalog", fake_scan)

    assert rawdata_catalog.cached_rawdata_catalog(tmp_path / "cached") is expected
    assert rawdata_catalog.cached_rawdata_catalog(tmp_path / "cached") is expected
    assert calls == 1


def test_incremental_scan_resumes_without_losing_pairs(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "batch" / "SAMPLE_R1.fastq.gz")
    _write(rawdata / "batch" / "SAMPLE_R2.fastq.gz")

    state: dict = {}
    catalog = None
    batches = 0
    while catalog is None:
        state, catalog = advance_rawdata_scan(
            rawdata,
            state,
            batch_entries=1,
            max_files=100,
            max_entries=100,
            max_depth=8,
            deadline_seconds=10,
        )
        batches += 1

    assert batches >= 3
    assert catalog["scan_limited"] is False
    assert catalog["summary"]["ready_dataset_count"] == 1
    assert [item["mate"] for item in catalog["datasets"][0]["files"]] == [1, 2]


def test_incremental_scan_reports_depth_limit_as_incomplete(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "level-one" / "level-two" / "SAMPLE_R1.fastq.gz")
    _write(rawdata / "level-one" / "level-two" / "SAMPLE_R2.fastq.gz")

    state: dict = {}
    catalog = None
    while catalog is None:
        state, catalog = advance_rawdata_scan(
            rawdata,
            state,
            batch_entries=10,
            max_files=100,
            max_entries=100,
            max_depth=1,
            deadline_seconds=10,
        )

    assert catalog["scan_limited"] is True
    assert catalog["datasets"] == []
    assert any(item["code"] == "RAWDATA_SCAN_DEPTH_LIMIT" for item in catalog["issues"])


def test_incremental_scan_can_finish_exactly_at_entry_and_file_budget(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "SAMPLE_R1.fastq.gz")
    _write(rawdata / "SAMPLE_R2.fastq.gz")

    state, catalog = advance_rawdata_scan(
        rawdata,
        {},
        batch_entries=10,
        max_files=2,
        max_entries=2,
        max_depth=8,
        deadline_seconds=10,
    )

    assert state == {}
    assert catalog is not None
    assert catalog["scan_limited"] is False
    assert catalog["summary"]["ready_dataset_count"] == 1


def test_persisted_index_records_changes_and_only_marks_missing_after_complete_scan(
    settings,
    tmp_path,
):
    rawdata = tmp_path / "rawdata"
    read1 = rawdata / "SAMPLE_R1.fastq.gz"
    read2 = rawdata / "SAMPLE_R2.fastq.gz"
    _write(read1)
    _write(read2)

    first = _finish_index(settings, rawdata)
    dataset = RawdataDatasetIndex.objects.get()
    original_digest = dataset.identity_digest
    assert first.status == RawdataScan.Status.SUCCEEDED
    assert list(dataset.events.values_list("action", flat=True)) == ["discovered"]

    _write(read1, b"changed reads\n")
    second = _finish_index(settings, rawdata)
    dataset.refresh_from_db()
    assert second.status == RawdataScan.Status.SUCCEEDED
    assert dataset.identity_digest != original_digest
    assert dataset.events.filter(action="changed").exists()

    read1.unlink()
    read2.unlink()
    settings.RAWDATA_SCAN_MAX_ENTRIES = 1
    limited = _finish_index(settings, rawdata)
    dataset.refresh_from_db()
    assert limited.status == RawdataScan.Status.SUCCEEDED
    assert dataset.active is False
    assert dataset.events.filter(action="missing").exists()


def test_limited_scan_does_not_deactivate_previous_dataset(settings, tmp_path):
    rawdata = tmp_path / "rawdata"
    read1 = rawdata / "SAMPLE_R1.fastq.gz"
    read2 = rawdata / "SAMPLE_R2.fastq.gz"
    _write(read1)
    _write(read2)
    _finish_index(settings, rawdata, batch_entries=10)
    dataset = RawdataDatasetIndex.objects.get()

    read1.unlink()
    read2.unlink()
    for index in range(3):
        (rawdata / f"empty-{index}").mkdir()
    settings.RAWDATA_SCAN_MAX_ENTRIES = 1
    scan, _ = queue_rawdata_scan(actor="tester", trigger="manual", root_value=rawdata)
    scan = run_rawdata_scan_batch(rawdata) or scan
    scan.refresh_from_db()
    dataset.refresh_from_db()

    assert scan.status == RawdataScan.Status.LIMITED
    assert dataset.active is True
    assert not dataset.events.filter(action="missing").exists()
    snapshot = indexed_rawdata_catalog(rawdata)
    assert snapshot["scan_limited"] is True
    assert snapshot["summary"]["ready_dataset_count"] == 1


def test_failed_scan_keeps_success_snapshot_and_periodic_retry_is_backed_off(
    settings,
    tmp_path,
):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "SAMPLE_R1.fastq.gz")
    _write(rawdata / "SAMPLE_R2.fastq.gz")
    succeeded = _finish_index(settings, rawdata, batch_entries=10)
    settings.RAWDATA_INDEX_INTERVAL_SECONDS = 300
    settings.RAWDATA_INDEX_STALE_SECONDS = 900
    failed = RawdataScan.objects.create(
        root_key=succeeded.root_key,
        status=RawdataScan.Status.FAILED,
        trigger="scheduled",
        actor="system",
        error="PermissionError: rawdata scan failed",
        finished_at=succeeded.finished_at,
    )

    catalog = indexed_rawdata_catalog(rawdata)
    queued, created = ensure_periodic_rawdata_scan(rawdata)

    assert catalog["summary"]["ready_dataset_count"] == 1
    assert catalog["index"]["latest_scan_id"] == str(failed.id)
    assert catalog["index"]["latest_status"] == "failed"
    assert any("扫描失败" in item for item in catalog["index"]["repair_suggestions"])
    assert queued is None
    assert created is False


def test_rawdata_catalog_api_is_read_only_and_disables_caching(client, settings, tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "SAMPLE_R1.fastq.gz")
    _write(rawdata / "SAMPLE_R2.fastq.gz")
    settings.ANALYSIS_RAWDATA_ROOT = rawdata

    queued = client.post("/api/v1/rawdata/scans", {}, format="json")
    duplicate = client.post("/api/v1/rawdata/scans", {}, format="json")
    assert queued.status_code == 202
    assert queued.data["created"] is True
    assert duplicate.status_code == 200
    assert duplicate.data["id"] == queued.data["id"]
    _finish_index(settings, rawdata)
    cooled_down = client.post("/api/v1/rawdata/scans", {}, format="json")
    assert cooled_down.status_code == 200
    assert cooled_down.data["created"] is False
    assert cooled_down.data["id"] == queued.data["id"]

    response = client.get("/api/v1/rawdata/catalog")

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response.data["root_directory"] == "workspace/rawdata"
    assert response.data["summary"]["ready_dataset_count"] == 1
    assert str(tmp_path) not in str(response.data)
    assert "identity" not in response.data["datasets"][0]["files"][0]
    assert response.data["index"]["latest_status"] == "succeeded"
    assert client.post("/api/v1/rawdata/catalog", {}, format="json").status_code == 405


def test_catalog_api_reads_snapshot_without_traversing_nas(
    client,
    monkeypatch,
    settings,
    tmp_path,
):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "SAMPLE_R1.fastq.gz")
    _write(rawdata / "SAMPLE_R2.fastq.gz")
    _finish_index(settings, rawdata)

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("request must not traverse rawdata")

    monkeypatch.setattr(rawdata_catalog, "scan_rawdata_catalog", forbidden_scan)
    response = client.get("/api/v1/rawdata/catalog")

    assert response.status_code == 200
    assert response.data["summary"]["ready_dataset_count"] == 1
    assert indexed_rawdata_catalog(rawdata)["root_status"] == "ready"

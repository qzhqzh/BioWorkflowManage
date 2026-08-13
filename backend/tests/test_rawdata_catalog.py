from __future__ import annotations

from pathlib import Path

import pytest

from workflows import rawdata_catalog
from workflows.rawdata_catalog import runnable_fastq_datasets, scan_rawdata_catalog


pytestmark = pytest.mark.django_db


def _write(path: Path, content: bytes = b"reads\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


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


def test_rawdata_catalog_api_is_read_only_and_disables_caching(client, settings, tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(rawdata / "SAMPLE_R1.fastq.gz")
    _write(rawdata / "SAMPLE_R2.fastq.gz")
    settings.ANALYSIS_RAWDATA_ROOT = rawdata

    response = client.get("/api/v1/rawdata/catalog")

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response.data["root_directory"] == "workspace/rawdata"
    assert response.data["summary"]["ready_dataset_count"] == 1
    assert str(tmp_path) not in str(response.data)
    assert "identity" not in response.data["datasets"][0]["files"][0]
    assert client.post("/api/v1/rawdata/catalog", {}, format="json").status_code == 405

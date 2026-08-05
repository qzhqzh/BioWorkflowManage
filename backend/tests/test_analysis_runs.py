from __future__ import annotations

import gzip
import json

import pytest

from workflows.models import AnalysisRun, WDLAsset, WDLSourceFile, WDLSourceRevision


pytestmark = pytest.mark.django_db


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
    settings.ANALYSIS_DATABASE_ROOT = databases
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
    assert run.events.get().message == "运行已进入队列。"


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
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="SolidTumorSingle",
        sample_id="SAMPLE01",
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        work_directory=str(run_directory),
        outputs={"outputs": {"SolidTumorSingle.summary": str(output)}},
    )

    detail = client.get(f"/api/v1/analysis-runs/{run.id}")
    assert detail.status_code == 200
    assert detail.data["outputs"][0]["name"] == "summary.txt"

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

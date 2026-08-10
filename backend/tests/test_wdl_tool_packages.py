import io
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from workflows.models import ToolDocument, WDLToolPackage, WDLToolPackageAuditEvent


QC_WDL = """version 1.0

task QC {
  input { File fastq }
  command <<<
    cp "~{fastq}" clean.fastq
  >>>
  output { File clean_fastq = "clean.fastq" }
  runtime { docker: "ubuntu:24.04" }
}
"""

ALIGN_WDL = """version 1.0

task Align {
  input { File fastq }
  command <<<
    cp "~{fastq}" aligned.bam
  >>>
  output { File bam = "aligned.bam" }
  runtime { docker: "ubuntu:24.04" }
}
"""


def package_archive(files: dict[str, str]) -> SimpleUploadedFile:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return SimpleUploadedFile(
        "solid-tools.zip",
        output.getvalue(),
        content_type="application/zip",
    )


@pytest.mark.django_db
def test_wdl_tool_package_import_version_export_and_archive():
    client = APIClient()
    files = {"task/qc.wdl": QC_WDL, "task/align.wdl": ALIGN_WDL}
    response = client.post(
        "/api/v1/wdl-packages",
        {
            "archive": package_archive(files),
            "name": "Solid tumor tools",
            "version": "1.0.0",
            "description": "Shared tasks",
            "tags": '["实体瘤", "hg38"]',
            "source_repository": "example/minwdl",
            "source_revision": "abc123",
            "note": "initial import",
        },
        format="multipart",
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["latest_version"]["version"] == "1.0.0"
    assert payload["latest_version"]["file_count"] == 2
    assert payload["latest_version"]["analysis"]["summary"] == {
        "task_count": 2,
        "workflow_count": 0,
        "import_count": 0,
        "error_count": 0,
    }
    assert set(payload["tags"]) == {"实体瘤", "hg38"}

    slug = payload["slug"]
    detail = client.get(f"/api/v1/wdl-packages/{slug}")
    assert detail.status_code == 200
    assert {item["path"] for item in detail.json()["latest_version"]["files"]} == set(files)

    version = client.get(f"/api/v1/wdl-packages/{slug}/versions/1.0.0")
    assert version.status_code == 200
    assert {item["content"] for item in version.json()["files"]} == set(files.values())

    exported = client.get(f"/api/v1/wdl-packages/{slug}/export?version=1.0.0")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == {"WDL_PACKAGE.json", *files}
        assert archive.read("task/qc.wdl").decode() == QC_WDL
        assert b'"version": "1.0.0"' in archive.read("WDL_PACKAGE.json")

    archived = client.patch(
        f"/api/v1/wdl-packages/{slug}",
        {"lifecycle": "archived", "note": "legacy only"},
        format="json",
    )
    assert archived.status_code == 200
    assert archived.json()["lifecycle"] == "archived"
    rejected = client.post(
        f"/api/v1/wdl-packages/{slug}/versions",
        {"archive": package_archive(files), "version": "1.1.0"},
        format="multipart",
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "WDL_TOOL_PACKAGE_ARCHIVED"
    assert WDLToolPackageAuditEvent.objects.filter(package__slug=slug).count() == 3


@pytest.mark.django_db
def test_wdl_tool_package_tasks_extract_to_idempotent_tool_drafts():
    client = APIClient()
    response = client.post(
        "/api/v1/wdl-packages",
        {
            "archive": package_archive(
                {"task/qc.wdl": QC_WDL, "task/align.wdl": ALIGN_WDL}
            ),
            "name": "Solid tumor tools",
            "slug": "solid-tumor-tools",
            "version": "1.0.0",
            "tags": "[]",
            "source_repository": "example/minwdl",
            "source_revision": "abc123",
        },
        format="multipart",
    )
    assert response.status_code == 201

    extracted = client.post(
        "/api/v1/wdl-packages/solid-tumor-tools/tasks/extract",
        {"version": "1.0.0"},
        format="json",
    )
    assert extracted.status_code == 201
    assert extracted.json()["task_count"] == 2
    assert extracted.json()["created_count"] == 2
    assert extracted.json()["reused_count"] == 0
    assert ToolDocument.objects.count() == 2
    qc = ToolDocument.objects.get(
        tool_id="solid_tumor_tools_task_qc_qc"
    )
    source = qc.draft_spec["metadata"]["source_wdl"]
    assert source["package_slug"] == "solid-tumor-tools"
    assert source["package_version"] == "1.0.0"
    assert source["file_path"] == "task/qc.wdl"
    assert source["task_name"] == "QC"
    assert source["source_digest"].startswith("sha256:")
    assert source["repository_revision"] == "abc123"
    assert qc.validation["status"] == "valid"
    registry = client.get("/api/v1/tools").json()["results"]
    qc_registry = next(
        item for item in registry if item["tool_id"] == qc.tool_id
    )
    assert qc_registry["source_wdl"]["package_version"] == "1.0.0"
    assert qc_registry["migration_warning_count"] == 1

    repeated = client.post(
        "/api/v1/wdl-packages/solid-tumor-tools/tasks/extract",
        {"version": "1.0.0"},
        format="json",
    )
    assert repeated.status_code == 200
    assert repeated.json()["created_count"] == 0
    assert repeated.json()["reused_count"] == 2
    assert ToolDocument.objects.count() == 2


@pytest.mark.django_db
def test_wdl_tool_package_version_is_idempotent_and_conflict_safe():
    client = APIClient()
    files = {"qc.wdl": QC_WDL}
    created = client.post(
        "/api/v1/wdl-packages",
        {
            "archive": package_archive(files),
            "name": "QC tools",
            "version": "2026.08",
            "tags": "[]",
        },
        format="multipart",
    )
    assert created.status_code == 201
    slug = created.json()["slug"]

    repeated = client.post(
        f"/api/v1/wdl-packages/{slug}/versions",
        {"archive": package_archive(files), "version": "2026.08"},
        format="multipart",
    )
    assert repeated.status_code == 200
    assert WDLToolPackage.objects.get(slug=slug).versions.count() == 1

    changed = client.post(
        f"/api/v1/wdl-packages/{slug}/versions",
        {
            "archive": package_archive({"qc.wdl": QC_WDL.replace("clean.fastq", "trimmed.fastq")}),
            "version": "2026.08",
        },
        format="multipart",
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "WDL_TOOL_PACKAGE_VERSION_CONFLICT"
    assert WDLToolPackage.objects.get(slug=slug).versions.count() == 1


@pytest.mark.django_db
def test_wdl_tool_package_requires_a_task():
    response = APIClient().post(
        "/api/v1/wdl-packages",
        {
            "archive": package_archive({"types.wdl": "version 1.0\nstruct Sample { String id }\n"}),
            "name": "Types only",
            "version": "1.0.0",
            "tags": "[]",
        },
        format="multipart",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WDL_TOOL_PACKAGE_HAS_NO_TASKS"
    assert WDLToolPackage.objects.count() == 0

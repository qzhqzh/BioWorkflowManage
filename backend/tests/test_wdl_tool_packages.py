import io
import json
import zipfile
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from compiler_core import canonical_digest
from workflows.models import ToolDocument, WDLToolPackage, WDLToolPackageAuditEvent
from workflows.wdl_packages import analyze_wdl_library


pytestmark = pytest.mark.usefixtures("auth_disabled")


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


ROOT = Path(__file__).resolve().parents[2]
CHAIN_FIXTURE = ROOT / "examples" / "phase1-fastp-bwa"
FASTP_FIXTURE = ROOT / "examples" / "phase1-fastp"


def _workflow_tool_package_source_fixture():
    graph = json.loads(
        (CHAIN_FIXTURE / "workflow-graph.json").read_text(encoding="utf-8")
    )
    tools = [
        json.loads(
            (FASTP_FIXTURE / "tool-fastp.json").read_text(encoding="utf-8")
        ),
        json.loads(
            (CHAIN_FIXTURE / "tool-bwa-mem.json").read_text(encoding="utf-8")
        ),
    ]
    return graph, tools


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
    assert payload["is_mine"] is True
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
    assert detail.json()["is_mine"] is True
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
def test_wdl_tool_package_can_start_from_single_wdl_file_without_zip():
    client = APIClient()

    response = client.post(
        "/api/v1/wdl-packages",
        {
            "name": "My first tools",
            "version": "1.0.0",
            "description": "Created in the browser",
            "tags": ["自建"],
            "files": [{"path": "tasks/qc.wdl", "content": QC_WDL}],
        },
        format="json",
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["latest_version"]["file_count"] == 1
    assert payload["latest_version"]["analysis"]["summary"]["task_count"] == 1
    assert payload["latest_version"]["files"][0]["path"] == "tasks/qc.wdl"
    assert payload["tags"] == ["自建"]
    version = client.get(
        f"/api/v1/wdl-packages/{payload['slug']}/versions/1.0.0"
    )
    assert version.status_code == 200
    assert version.json()["files"][0]["content"] == QC_WDL


@pytest.mark.django_db
def test_wdl_tool_package_preview_is_read_only_and_can_be_confirmed():
    client = APIClient()
    files = [{"path": "tasks/qc.wdl", "content": QC_WDL}]

    preview = client.post(
        "/api/v1/wdl-packages/preview",
        {"files": files},
        format="json",
    )

    assert preview.status_code == 200
    assert preview.json()["can_publish"] is True
    assert preview.json()["preview_digest"].startswith("sha256:")
    assert preview.json()["analysis"]["summary"]["task_count"] == 1
    assert preview.json()["analysis"]["tasks"][0]["name"] == "QC"
    assert WDLToolPackage.objects.count() == 0

    created = client.post(
        "/api/v1/wdl-packages",
        {
            "name": "Previewed QC tools",
            "version": "1.0.0",
            "tags": [],
            "files": files,
            "preview_digest": preview.json()["preview_digest"],
            "confirm_preview": True,
        },
        format="json",
    )

    assert created.status_code == 201
    assert created.json()["latest_version"]["digest"] == preview.json()["preview_digest"]


@pytest.mark.django_db
def test_wdl_tool_package_preview_reports_diagnostics_without_writing():
    preview = APIClient().post(
        "/api/v1/wdl-packages/preview",
        {
            "files": [
                {
                    "path": "tasks/qc.wdl",
                    "content": 'version 1.0\nimport "missing.wdl"\n' + QC_WDL.removeprefix("version 1.0\n"),
                }
            ]
        },
        format="json",
    )

    assert preview.status_code == 200
    assert preview.json()["can_publish"] is False
    assert preview.json()["analysis"]["status"] == "invalid"
    assert preview.json()["analysis"]["summary"]["error_count"] > 0
    assert any(
        item["code"] == "WDL_IMPORT_MISSING"
        for item in preview.json()["analysis"]["diagnostics"]
    )
    assert WDLToolPackage.objects.count() == 0


@pytest.mark.django_db
def test_wdl_tool_package_rejects_invalid_content_without_preview_confirmation():
    response = APIClient().post(
        "/api/v1/wdl-packages",
        {
            "name": "Invalid legacy import",
            "version": "1.0.0",
            "tags": [],
            "files": [
                {
                    "path": "tasks/qc.wdl",
                    "content": 'version 1.0\nimport "missing.wdl"\n'
                    + QC_WDL.removeprefix("version 1.0\n"),
                }
            ],
        },
        format="json",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "WDL_TOOL_PACKAGE_PREVIEW_INVALID"
    assert WDLToolPackage.objects.count() == 0


@pytest.mark.django_db
def test_wdl_tool_package_rejects_stale_confirmed_preview():
    client = APIClient()
    preview = client.post(
        "/api/v1/wdl-packages/preview",
        {"files": [{"path": "tasks/qc.wdl", "content": QC_WDL}]},
        format="json",
    )

    response = client.post(
        "/api/v1/wdl-packages",
        {
            "name": "Changed after preview",
            "version": "1.0.0",
            "tags": [],
            "files": [
                {
                    "path": "tasks/qc.wdl",
                    "content": QC_WDL.replace("clean.fastq", "trimmed.fastq"),
                }
            ],
            "preview_digest": preview.json()["preview_digest"],
            "confirm_preview": True,
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WDL_TOOL_PACKAGE_PREVIEW_STALE"
    assert WDLToolPackage.objects.count() == 0


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

    unprotected_replace = client.post(
        "/api/v1/wdl-packages/solid-tumor-tools/tasks/extract",
        {"version": "1.0.0", "replace": True},
        format="json",
    )
    assert unprotected_replace.status_code == 428
    assert (
        unprotected_replace.json()["error"]["code"]
        == "TOOL_DRAFT_PRECONDITION_REQUIRED"
    )

    drafts = list(ToolDocument.objects.all())
    base_drafts = {
        item.tool_id: {
            "version": item.draft_version,
            "digest": canonical_digest(item.draft_spec),
        }
        for item in drafts
    }
    replaced = client.post(
        "/api/v1/wdl-packages/solid-tumor-tools/tasks/extract",
        {
            "version": "1.0.0",
            "replace": True,
            "base_drafts": base_drafts,
        },
        format="json",
    )
    assert replaced.status_code == 200
    assert replaced.json()["created_count"] == 0
    assert set(ToolDocument.objects.values_list("draft_version", flat=True)) == {2}

    stale_replace = client.post(
        "/api/v1/wdl-packages/solid-tumor-tools/tasks/extract",
        {
            "version": "1.0.0",
            "replace": True,
            "base_drafts": base_drafts,
        },
        format="json",
    )
    assert stale_replace.status_code == 409
    assert stale_replace.json()["error"]["code"] == "TOOL_DRAFT_CONFLICT"


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


@pytest.mark.django_db
def test_workflow_tool_package_source_enforces_document_and_tool_preconditions():
    client = APIClient()
    graph, tools = _workflow_tool_package_source_fixture()
    saved = client.put(
        "/api/v1/editor/workflows/fastp_bwa_demo",
        {
            "name": graph["name"],
            "description": graph["description"],
            "workflow_graph": graph,
            "tool_specs": tools,
            "editor_document": {"nodes": []},
        },
        format="json",
    )
    assert saved.status_code == 200
    tool_digests = [canonical_digest(tool) for tool in tools]
    endpoint = "/api/v1/editor/workflows/fastp_bwa_demo/tool-package-source"

    missing_base = client.post(
        endpoint,
        {"tool_digests": tool_digests},
        format="json",
    )
    assert missing_base.status_code == 428

    stale_base = client.post(
        endpoint,
        {
            "base_document_version": saved.data["document_version"],
            "base_document_digest": "sha256:" + "0" * 64,
            "tool_digests": tool_digests,
        },
        format="json",
    )
    assert stale_base.status_code == 409

    unreferenced = client.post(
        endpoint,
        {
            "base_document_version": saved.data["document_version"],
            "base_document_digest": saved.data["document_digest"],
            "tool_digests": ["sha256:" + "f" * 64],
        },
        format="json",
    )
    assert unreferenced.status_code == 422


@pytest.mark.django_db
def test_workflow_tool_package_source_returns_task_only_wdl_for_each_tool():
    client = APIClient()
    graph, tools = _workflow_tool_package_source_fixture()
    saved = client.put(
        "/api/v1/editor/workflows/fastp_bwa_demo",
        {
            "name": graph["name"],
            "description": graph["description"],
            "workflow_graph": graph,
            "tool_specs": tools,
            "editor_document": {"nodes": []},
        },
        format="json",
    )
    assert saved.status_code == 200
    tool_digests = [canonical_digest(tool) for tool in tools]
    response = client.post(
        "/api/v1/editor/workflows/fastp_bwa_demo/tool-package-source",
        {
            "base_document_version": saved.data["document_version"],
            "base_document_digest": saved.data["document_digest"],
            "tool_digests": tool_digests,
        },
        format="json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workflow"] == {
        "slug": "fastp_bwa_demo",
        "name": graph["name"],
        "document_version": saved.data["document_version"],
        "document_digest": saved.data["document_digest"],
    }
    files = payload["files"]
    assert {item["tool_digest"] for item in files} == set(tool_digests)
    assert {
        (item["tool_id"], item["tool_version"], item["tool_digest"])
        for item in files
    } == {
        (tool["id"], tool["tool_version"], digest)
        for tool, digest in zip(tools, tool_digests)
    }
    assert {item["path"] for item in files} == {
        "tasks/fastp.wdl",
        "tasks/bwa_mem.wdl",
    }
    assert all(
        {"path", "content", "tool_id", "tool_version", "tool_digest"}
        <= set(item)
        for item in files
    )
    assert all("workflow " not in item["content"] for item in files)

    analysis = analyze_wdl_library(
        {item["path"]: item["content"] for item in files}
    )
    assert analysis["status"] == "valid"
    assert analysis["summary"]["task_count"] == 2
    assert analysis["summary"]["workflow_count"] == 0
    assert payload["analysis"]["summary"]["task_count"] == 2

import io
import json
import zipfile

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from workflows.models import (
    ToolDocument,
    WDLAsset,
    WDLSourcePackageReference,
    WDLSourceRevision,
    WDLToolPackage,
    WDLToolPackageFile,
    WDLToolPackageVersion,
)
from workflows.wdl_packages import analyze_wdl_library, digest, package_digest


TASK_WDL = """version 1.0

task Hello {
  input { String name }
  command <<<
    echo "~{name}" > greeting.txt
  >>>
  output { File greeting = "greeting.txt" }
  runtime { docker: "ubuntu:24.04" }
}
"""

MAIN_WDL = """version 1.0

import "task/hello.wdl" as hello

workflow Greeting {
  input { String name }
  call hello.Hello { input: name = name }
  output { File greeting = Hello.greeting }
}
"""


def create_package() -> WDLToolPackageVersion:
    files = {"task/hello.wdl": TASK_WDL}
    package = WDLToolPackage.objects.create(
        slug="greeting-tools",
        name="Greeting tools",
        created_by="tester",
    )
    package_version = WDLToolPackageVersion.objects.create(
        package=package,
        version="1.0.0",
        digest=package_digest(files),
        actor="tester",
        analysis=analyze_wdl_library(files),
    )
    WDLToolPackageFile.objects.create(
        package_version=package_version,
        path="task/hello.wdl",
        content=TASK_WDL,
        digest=digest(TASK_WDL),
        analysis=package_version.analysis["files"][0],
    )
    return package_version


def create_asset(client: APIClient) -> dict:
    response = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Greeting workflow",
            "entrypoint": "main.wdl",
            "files": [
                {"path": "main.wdl", "content": MAIN_WDL},
                {"path": "task/hello.wdl", "content": TASK_WDL},
            ],
        },
        format="json",
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.django_db
def test_migration_links_exact_package_without_mutating_history_and_exports(capsys):
    package_version = create_package()
    client = APIClient()
    asset_payload = create_asset(client)
    asset = WDLAsset.objects.get(slug=asset_payload["slug"])
    original = asset.source_revisions.get(version=1)
    original_file_ids = list(original.files.order_by("path").values_list("id", flat=True))

    call_command(
        "migrate_wdl_package_references",
        package_slug="greeting-tools",
        package_version="1.0.0",
    )
    assert "CANDIDATE" in capsys.readouterr().out
    assert asset.source_revisions.count() == 1
    assert WDLSourcePackageReference.objects.count() == 0

    call_command(
        "migrate_wdl_package_references",
        package_slug="greeting-tools",
        package_version="1.0.0",
        apply=True,
        actor="tester",
    )
    assert "MIGRATED" in capsys.readouterr().out
    asset.refresh_from_db()
    linked = asset.source_revisions.get(version=2)
    assert linked.operation == WDLSourceRevision.Operation.PACKAGE_LINK
    assert list(linked.files.values_list("path", flat=True)) == ["main.wdl"]
    reference = linked.package_references.get()
    assert reference.package_version == package_version
    assert reference.digest == package_version.digest
    assert reference.mount_prefix == ""
    assert list(original.files.order_by("path").values_list("id", flat=True)) == original_file_ids

    detail = client.get(f"/api/v1/wdl-assets/{asset.slug}")
    assert detail.status_code == 200
    revision_payload = detail.json()["current_revision"]
    assert revision_payload["version"] == 2
    assert revision_payload["package_references"][0]["version"] == "1.0.0"
    file_payloads = {item["path"]: item for item in revision_payload["files"]}
    assert file_payloads["main.wdl"]["origin"] == "asset"
    assert file_payloads["task/hello.wdl"]["origin"] == "package"
    assert file_payloads["task/hello.wdl"]["read_only"] is True

    package_detail = client.get("/api/v1/wdl-packages/greeting-tools")
    assert package_detail.status_code == 200
    assert package_detail.json()["reference_count"] == 1
    assert package_detail.json()["references"][0]["asset_slug"] == asset.slug

    exported = client.get(f"/api/v1/wdl-assets/{asset.slug}/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read("main.wdl").decode() == MAIN_WDL
        assert archive.read("task/hello.wdl").decode() == TASK_WDL
        manifest = json.loads(archive.read("MANIFEST.json"))
        references = manifest["bioworkflow"]["packageReferences"]
        assert references[0]["package_slug"] == "greeting-tools"
        assert references[0]["digest"] == package_version.digest

    imported = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/tasks/import",
        {"file_path": "task/hello.wdl", "task_name": "Hello"},
        format="json",
    )
    assert imported.status_code == 201
    assert ToolDocument.objects.filter(tool_id=imported.json()["tool_id"]).exists()

    call_command(
        "migrate_wdl_package_references",
        package_slug="greeting-tools",
        package_version="1.0.0",
        apply=True,
    )
    assert asset.source_revisions.count() == 2


@pytest.mark.django_db
def test_revision_inherits_reference_and_rejects_digest_or_mount_collision():
    package_version = create_package()
    client = APIClient()
    asset = create_asset(client)
    call_command(
        "migrate_wdl_package_references",
        package_slug="greeting-tools",
        package_version="1.0.0",
        apply=True,
    )

    updated_main = MAIN_WDL.replace("workflow Greeting", "workflow GreetingUpdated")
    saved = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/revisions",
        {
            "entrypoint": "main.wdl",
            "files": [{"path": "main.wdl", "content": updated_main}],
            "operation": "edit",
        },
        format="json",
    )
    assert saved.status_code == 201
    assert saved.json()["version"] == 3
    assert saved.json()["package_references"][0]["digest"] == package_version.digest
    revision = WDLSourceRevision.objects.get(asset__slug=asset["slug"], version=3)
    assert list(revision.files.values_list("path", flat=True)) == ["main.wdl"]
    assert revision.package_references.count() == 1

    digest_mismatch = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/revisions",
        {
            "entrypoint": "main.wdl",
            "files": [{"path": "main.wdl", "content": updated_main}],
            "package_references": [
                {
                    "package_slug": "greeting-tools",
                    "version": "1.0.0",
                    "digest": "sha256:wrong",
                    "mount_prefix": "packages/greeting-tools/1.0.0",
                }
            ],
        },
        format="json",
    )
    assert digest_mismatch.status_code == 400
    assert digest_mismatch.json()["error"]["code"] == "WDL_TOOL_PACKAGE_DIGEST_MISMATCH"

    collision = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/revisions",
        {
            "entrypoint": "main.wdl",
            "files": [
                {"path": "main.wdl", "content": updated_main},
                {"path": "task/hello.wdl", "content": TASK_WDL},
            ],
            "package_references": [
                {
                    "package_slug": "greeting-tools",
                    "version": "1.0.0",
                    "digest": package_version.digest,
                    "mount_prefix": "",
                }
            ],
        },
        format="json",
    )
    assert collision.status_code == 400
    assert collision.json()["error"]["code"] == "WDL_PACKAGE_MOUNT_CONFLICT"

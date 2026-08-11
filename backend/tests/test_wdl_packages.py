import io
import zipfile

import pytest
from rest_framework.test import APIClient

from workflows.models import ToolDocument, WDLAuditEvent, WDLAsset


TASK_WDL = """version 1.0

task Hello {
  input {
    String name
  }
  command <<<
    echo "~{name}" > greeting.txt
  >>>
  output {
    File greeting = "greeting.txt"
    Pair[File, File] pair = ("greeting.txt", "greeting.txt")
  }
  runtime {
    docker: "ubuntu:24.04"
  }
}
"""

MAIN_WDL = """version 1.0

import "task/hello.wdl" as hello

workflow Greeting {
  input {
    String name
  }
  call hello.Hello { input: name = name }
  output {
    File greeting = Hello.greeting
  }
}
"""


@pytest.mark.django_db
def test_multifile_asset_revision_export_and_task_import():
    client = APIClient()
    response = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Greeting package",
            "entrypoint": "main.wdl",
            "files": [
                {"path": "main.wdl", "content": MAIN_WDL},
                {"path": "task/hello.wdl", "content": TASK_WDL},
            ],
            "source_repository": "example/tumor_wdl",
            "source_revision": "abc123",
            "tags": ["test"],
        },
        format="json",
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["file_count"] == 2
    assert payload["current_revision"]["entrypoint"] == "main.wdl"
    assert {item["path"] for item in payload["current_revision"]["files"]} == {
        "main.wdl",
        "task/hello.wdl",
    }
    assert payload["current_revision"]["analysis"]["summary"]["task_count"] == 1

    slug = payload["slug"]
    updated_task = TASK_WDL.replace('echo "~{name}"', 'printf "%s\\n" "~{name}"')
    revision_response = client.post(
        f"/api/v1/wdl-assets/{slug}/revisions",
        {
            "entrypoint": "main.wdl",
            "files": [
                {"path": "main.wdl", "content": MAIN_WDL},
                {"path": "task/hello.wdl", "content": updated_task},
            ],
            "operation": "edit",
            "note": "update command",
            "base_version": payload["current_revision"]["version"],
            "base_digest": payload["current_revision"]["digest"],
        },
        format="json",
    )
    assert revision_response.status_code == 201
    assert revision_response.json()["version"] == 2
    assert "task/hello.wdl (before)" in revision_response.json()["diff"]

    export_response = client.get(f"/api/v1/wdl-assets/{slug}/export")
    assert export_response.status_code == 200
    assert export_response["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(export_response.content)) as archive:
        assert archive.read("main.wdl").decode() == MAIN_WDL
        assert archive.read("task/hello.wdl").decode() == updated_task
        assert b'"mainWorkflowURL": "main.wdl"' in archive.read("MANIFEST.json")

    import_response = client.post(
        f"/api/v1/wdl-assets/{slug}/tasks/import",
        {"version": 2, "file_path": "task/hello.wdl", "task_name": "Hello"},
        format="json",
    )
    assert import_response.status_code == 201
    tool = ToolDocument.objects.get(tool_id=import_response.json()["tool_id"])
    assert tool.validation["status"] == "valid"
    assert tool.draft_spec["outputs"][1]["wdl_type"] == "Pair[File,File]"
    assert tool.draft_spec["outputs"][0]["capture"]["mode"] == "expression"
    assert tool.draft_spec["metadata"]["source_wdl"]["file_path"] == "task/hello.wdl"

    idempotent_response = client.post(
        f"/api/v1/wdl-assets/{slug}/tasks/import",
        {"version": 2, "file_path": "task/hello.wdl", "task_name": "Hello"},
        format="json",
    )
    assert idempotent_response.status_code == 200
    assert WDLAuditEvent.objects.filter(asset__slug=slug, action="tool_import").count() == 2


@pytest.mark.django_db
def test_multifile_package_rejects_parent_path():
    response = APIClient().post(
        "/api/v1/wdl-assets",
        {
            "name": "Unsafe package",
            "entrypoint": "main.wdl",
            "files": [
                {"path": "main.wdl", "content": MAIN_WDL},
                {"path": "../task/hello.wdl", "content": TASK_WDL},
            ],
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WDL_PACKAGE_PATH_INVALID"
    assert WDLAsset.objects.count() == 0

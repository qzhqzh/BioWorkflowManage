import subprocess
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from workflows import wdl_assets
from workflows.models import WDLAuditEvent, WDLAsset, WDLSourceRevision, WDLTag


pytestmark = pytest.mark.usefixtures("auth_disabled")


WDL_SOURCE = """version 1.0

task hello {
input {
String name
}
command <<<
echo "~{name}" > greeting.txt
>>>
output {
File greeting = "greeting.txt"
}
}

workflow greeting {
input {
String name
}
call hello {
input:
name = name
}
output {
File greeting = hello.greeting
}
}
"""

CRLF_WDL_SOURCE = (
    "version 1.0\r\n"
    "# 聚合多个输入文件\r\n"
    "    workflow AggregateCounts {\r\n"
    "input {\r\n"
    "Array[File] input_files\r\n"
    "}\r\n"
    "scatter (file in input_files) {\r\n"
    "call CountLines as counter {\r\n"
    "input:\r\n"
    "input_file = file\r\n"
    "}\r\n"
    "}\r\n"
    "}\r\n"
    "\r\n"
    "task CountLines {\r\n"
    "input {\r\n"
    "File input_file\r\n"
    "}\r\n"
    "command {\r\n"
    "echo ~{input_file} > result.txt\r\n"
    "}\r\n"
    "output {\r\n"
    'File result = "result.txt"\r\n'
    "}\r\n"
    "}\r\n"
)

SPROCKET_FORMATTED_WDL_SOURCE = """version 1.0

task hello {
  input {
    String name
  }

  command <<<
    echo "~{name}" > greeting.txt
  >>>

  output {
    File greeting = "greeting.txt"
  }
}

workflow greeting {
  input {
    String name
  }

  call hello { input:
    name = name
  }

  output {
    File greeting = hello.greeting
  }
}
"""

SPROCKET_FORMATTED_CRLF_WDL_SOURCE = """version 1.0

# 聚合多个输入文件
workflow AggregateCounts {
  input {
    Array[File] input_files
  }

  scatter (file in input_files) {
    call CountLines as counter { input:
      input_file = file
    }
  }
}

task CountLines {
  input {
    File input_file
  }

  command <<<
    echo ~{input_file} > result.txt
  >>>

  output {
    File result = "result.txt"
  }
}
"""

MESSY_WDL_SOURCE = """version 1.0
task hello{ input{String    name
Int count=1} command <<<

  echo    "~{name}"    > output.txt

>>> output{File out="output.txt"} runtime{docker:"ubuntu:22.04"}}
workflow main{input{String  who} call hello{input:name=who,count=2}output{File result=hello.out}}
"""

SPROCKET_FORMATTED_MESSY_WDL_SOURCE = """version 1.0

task hello {
  input {
    String name
    Int count = 1
  }

  command <<<

    echo    "~{name}"    > output.txt
  >>>

  output {
    File out = "output.txt"
  }

  runtime {
    docker: "ubuntu:22.04"
  }
}

workflow main {
  input {
    String who
  }

  call hello { input:
    name = who,
    count = 2
  }

  output {
    File result = hello.out
  }
}
"""


@pytest.fixture(autouse=True)
def mock_sprocket_process(monkeypatch):
    formatted_sources = {
        WDL_SOURCE: SPROCKET_FORMATTED_WDL_SOURCE,
        SPROCKET_FORMATTED_WDL_SOURCE: SPROCKET_FORMATTED_WDL_SOURCE,
        CRLF_WDL_SOURCE.replace("\r\n", "\n"): SPROCKET_FORMATTED_CRLF_WDL_SOURCE,
        SPROCKET_FORMATTED_CRLF_WDL_SOURCE: SPROCKET_FORMATTED_CRLF_WDL_SOURCE,
        MESSY_WDL_SOURCE: SPROCKET_FORMATTED_MESSY_WDL_SOURCE,
        SPROCKET_FORMATTED_MESSY_WDL_SOURCE: SPROCKET_FORMATTED_MESSY_WDL_SOURCE,
    }

    def fake_run(command, **kwargs):
        content = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=formatted_sources.get(content, content),
            stderr="",
        )

    monkeypatch.setattr(wdl_assets.subprocess, "run", fake_run)


@pytest.mark.django_db
def test_imported_wdl_asset_keeps_analysis_tags_and_audit_history():
    client = APIClient()

    response = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "实体瘤分析",
            "filename": "solid-tumor.wdl",
            "content": WDL_SOURCE,
            "tags": ["实体瘤", "hg38"],
            "note": "从生产目录导入",
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["current_revision"]["version"] == 1
    assert response.data["current_revision"]["operation"] == "import"
    assert response.data["current_revision"]["analysis"]["status"] == "valid"
    assert response.data["current_revision"]["analysis"]["summary"] == {
        "task_count": 1,
        "workflow_count": 1,
        "import_count": 0,
        "error_count": 0,
    }
    assert set(response.data["tags"]) == {"实体瘤", "hg38"}
    assert response.data["audit_events"][0]["actor"] == "local-user"
    assert response.data["audit_events"][0]["note"] == "从生产目录导入"

    filtered = client.get("/api/v1/wdl-assets", {"tag": "hg38"})
    assert filtered.status_code == 200
    assert [item["slug"] for item in filtered.data["results"]] == [
        response.data["slug"]
    ]
    listed = filtered.data["results"][0]
    assert listed["maintenance_status"] == "ready"
    assert listed["maintenance_counts"] == {"errors": 0, "warnings": 0}
    assert listed["latest_activity"]["actor"] == "local-user"
    assert listed["latest_activity"]["action"] == "import"
    assert listed["latest_activity"]["note"] == "从生产目录导入"


@pytest.mark.django_db
def test_wdl_asset_list_reports_invalid_revision_as_maintenance_error():
    client = APIClient()

    response = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "需要修复的流程",
            "filename": "broken.wdl",
            "content": "version 1.0\nworkflow broken { call missing }\n",
            "note": "先纳入资产台账",
        },
        format="json",
    )

    assert response.status_code == 201
    listed = client.get("/api/v1/wdl-assets").data["results"][0]
    assert listed["maintenance_status"] == "error"
    assert listed["maintenance_counts"]["errors"] >= 1
    assert listed["latest_activity"]["note"] == "先纳入资产台账"


@pytest.mark.django_db
def test_format_preview_and_saved_revision_record_diff_note_and_actor():
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Greeting workflow",
            "filename": "greeting.wdl",
            "content": WDL_SOURCE,
            "tags": ["hg19"],
        },
        format="json",
    )
    slug = imported.data["slug"]

    preview = client.post(
        f"/api/v1/wdl-assets/{slug}/format",
        {"content": WDL_SOURCE},
        format="json",
    )

    assert preview.status_code == 200
    assert preview.data["changed"] is True
    assert "@@" in preview.data["diff"]
    assert "\n  input {\n    String name\n  }\n" in preview.data["content"]
    assert (
        '\n  command <<<\n    echo "~{name}" > greeting.txt\n  >>>\n'
        in preview.data["content"]
    )
    assert "\n  call hello { input:\n    name = name\n  }\n" in preview.data[
        "content"
    ]
    repeated_preview = client.post(
        f"/api/v1/wdl-assets/{slug}/format",
        {"content": preview.data["content"]},
        format="json",
    )
    assert repeated_preview.status_code == 200
    assert repeated_preview.data["changed"] is False
    assert repeated_preview.data["diff"] == ""

    saved = client.post(
        f"/api/v1/wdl-assets/{slug}/revisions",
        {
            "content": preview.data["content"],
            "operation": "format",
            "note": "统一两空格缩进",
            "base_version": imported.data["current_revision"]["version"],
            "base_digest": imported.data["current_revision"]["digest"],
        },
        format="json",
    )

    assert saved.status_code == 201
    assert saved.data["version"] == 2
    assert saved.data["operation"] == "format"
    assert saved.data["actor"] == "local-user"
    assert saved.data["note"] == "统一两空格缩进"
    assert saved.data["diff"]
    event = WDLAuditEvent.objects.get(action="format")
    assert event.revision.version == 2
    assert event.diff == saved.data["diff"]


@pytest.mark.django_db
def test_stale_wdl_revision_is_rejected_without_overwriting_latest_source():
    user_model = get_user_model()
    editor = user_model.objects.create_user(username="editor")
    reviewer = user_model.objects.create_user(username="reviewer")
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Collaborative workflow",
            "filename": "collaborative.wdl",
            "content": WDL_SOURCE,
        },
        format="json",
    )
    slug = imported.data["slug"]
    base_revision = imported.data["current_revision"]

    client.force_authenticate(editor)
    latest_source = WDL_SOURCE.replace("echo \"~{name}\"", "echo \"editor: ~{name}\"")
    saved = client.post(
        f"/api/v1/wdl-assets/{slug}/revisions",
        {
            "content": latest_source,
            "operation": "edit",
            "note": "补充输出前缀",
            "base_version": base_revision["version"],
            "base_digest": base_revision["digest"],
        },
        format="json",
    )
    assert saved.status_code == 201
    assert saved.data["version"] == 2

    client.force_authenticate(reviewer)
    stale_source = WDL_SOURCE.replace("greeting.txt", "reviewer.txt")
    conflict = client.post(
        f"/api/v1/wdl-assets/{slug}/revisions",
        {
            "content": stale_source,
            "operation": "edit",
            "note": "修改输出文件",
            "base_version": base_revision["version"],
            "base_digest": base_revision["digest"],
        },
        format="json",
    )

    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "WDL_REVISION_CONFLICT"
    assert conflict.data["error"]["details"]["actor"] == "editor"
    assert conflict.data["error"]["details"]["note"] == "补充输出前缀"
    assert conflict.data["current_revision"]["version"] == 2
    assert conflict.data["current_revision"]["content"] == latest_source
    assert WDLSourceRevision.objects.filter(asset__slug=slug).count() == 2
    assert not WDLSourceRevision.objects.filter(content=stale_source).exists()


@pytest.mark.django_db
def test_format_normalizes_crlf_and_reindents_nested_wdl():
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "CRLF workflow",
            "filename": "crlf.wdl",
            "content": CRLF_WDL_SOURCE,
        },
        format="json",
    )
    slug = imported.data["slug"]

    assert imported.status_code == 201
    assert imported.data["current_revision"]["analysis"]["status"] == "valid"

    preview = client.post(
        f"/api/v1/wdl-assets/{slug}/format",
        {"content": CRLF_WDL_SOURCE},
        format="json",
    )

    assert preview.status_code == 200
    assert preview.data["changed"] is True
    assert "\r" not in preview.data["content"]
    assert (
        "\nworkflow AggregateCounts {\n"
        "  input {\n"
        "    Array[File] input_files\n"
        "  }\n"
        "\n"
        "  scatter (file in input_files) {\n"
        "    call CountLines as counter { input:\n"
        "      input_file = file\n"
        "    }\n"
        "  }\n"
        "}\n"
        in preview.data["content"]
    )
    assert (
        "\ntask CountLines {\n"
        "  input {\n"
        "    File input_file\n"
        "  }\n"
        "\n"
        "  command <<<\n"
        "    echo ~{input_file} > result.txt\n"
        "  >>>\n"
        in preview.data["content"]
    )
    assert preview.data["diff"].count("@@") <= 2


def test_sprocket_formatter_normalizes_spacing_and_adds_structural_blank_lines():
    formatted = wdl_assets.format_wdl(MESSY_WDL_SOURCE, "messy.wdl")

    assert formatted == SPROCKET_FORMATTED_MESSY_WDL_SOURCE
    assert "String name\n    Int count = 1" in formatted
    assert "\n  }\n\n  command <<<\n" in formatted
    assert 'echo    "~{name}"    > output.txt' in formatted
    assert wdl_assets.format_wdl(formatted, "messy.wdl") == formatted


def test_sprocket_adapter_uses_fixed_cli_contract(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=SPROCKET_FORMATTED_WDL_SOURCE,
            stderr="",
        )

    monkeypatch.setattr(wdl_assets.subprocess, "run", fake_run)
    formatted = wdl_assets._format_wdl_with_sprocket(WDL_SOURCE, "workflow.wdl")

    assert formatted == SPROCKET_FORMATTED_WDL_SOURCE
    assert captured["command"][1:6] == [
        "format",
        "--skip-config-search",
        "--config",
        str(wdl_assets.settings.SPROCKET_FORMAT_CONFIG),
        "view",
    ]
    assert captured["command"][-1].endswith("/source.wdl")
    assert captured["kwargs"]["timeout"] == wdl_assets.settings.SPROCKET_FORMAT_TIMEOUT_SECONDS
    assert captured["kwargs"]["check"] is False


def test_draft2_formatter_keeps_legacy_compatibility(monkeypatch):
    source = """task hello {
command {
echo hello
}
}
"""

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Sprocket must not receive unversioned draft-2 WDL.")

    monkeypatch.setattr(wdl_assets, "_format_wdl_with_sprocket", fail_if_called)
    formatted = wdl_assets.format_wdl(source, "draft2.wdl")

    assert formatted == """task hello {
  command {
    echo hello
  }
}
"""


@pytest.mark.django_db
def test_formatter_unavailable_returns_service_unavailable(monkeypatch):
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Formatter unavailable",
            "filename": "unavailable.wdl",
            "content": WDL_SOURCE,
        },
        format="json",
    )

    def unavailable(*args, **kwargs):
        raise wdl_assets.WDLFormatterUnavailable("formatter unavailable")

    monkeypatch.setattr(wdl_assets, "_format_wdl_with_sprocket", unavailable)
    response = client.post(
        f"/api/v1/wdl-assets/{imported.data['slug']}/format",
        {"content": WDL_SOURCE},
        format="json",
    )

    assert response.status_code == 503
    assert response.data["error"] == {
        "code": "WDL_FORMATTER_UNAVAILABLE",
        "message": "formatter unavailable",
    }


@pytest.mark.django_db
def test_metadata_and_tag_changes_are_audited_without_rewriting_source():
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Blood workflow",
            "filename": "blood.wdl",
            "content": WDL_SOURCE,
            "tags": ["血液肿瘤", "hg19"],
        },
        format="json",
    )
    slug = imported.data["slug"]

    updated = client.patch(
        f"/api/v1/wdl-assets/{slug}",
        {
            "name": "Blood workflow hg38",
            "description": "升级后的血液肿瘤流程",
            "lifecycle": "migrating",
            "tags": ["血液肿瘤", "hg38"],
            "note": "参考基因组升级",
            "base_metadata_version": imported.data["metadata_version"],
        },
        format="json",
    )

    assert updated.status_code == 200
    assert updated.data["metadata_version"] == 2
    assert updated.data["name"] == "Blood workflow hg38"
    assert updated.data["description"] == "升级后的血液肿瘤流程"
    assert updated.data["lifecycle"] == "migrating"
    assert set(updated.data["tags"]) == {"血液肿瘤", "hg38"}
    assert updated.data["revision_count"] == 1
    event = WDLAuditEvent.objects.get(action="metadata_update")
    assert event.note == "参考基因组升级"
    assert event.changes["name"] == {
        "before": "Blood workflow",
        "after": "Blood workflow hg38",
    }
    assert event.changes["tags"] == {
        "before": ["hg19", "血液肿瘤"],
        "after": ["血液肿瘤", "hg38"],
    }


@pytest.mark.django_db
def test_stale_wdl_metadata_is_rejected_and_returns_latest_asset():
    user_model = get_user_model()
    editor = user_model.objects.create_user(username="metadata-editor")
    reviewer = user_model.objects.create_user(username="metadata-reviewer")
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Shared workflow",
            "filename": "shared.wdl",
            "content": WDL_SOURCE,
            "tags": ["hg19"],
        },
        format="json",
    )
    slug = imported.data["slug"]
    assert imported.data["metadata_version"] == 1

    client.force_authenticate(editor)
    updated = client.patch(
        f"/api/v1/wdl-assets/{slug}",
        {
            "name": "Shared workflow hg38",
            "base_metadata_version": 1,
            "note": "升级流程标题",
        },
        format="json",
    )
    assert updated.status_code == 200
    assert updated.data["metadata_version"] == 2

    client.force_authenticate(reviewer)
    conflict = client.patch(
        f"/api/v1/wdl-assets/{slug}",
        {
            "description": "过期页面上的说明",
            "base_metadata_version": 1,
            "note": "补充说明",
        },
        format="json",
    )

    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "WDL_METADATA_CONFLICT"
    assert conflict.data["error"]["details"]["actor"] == "metadata-editor"
    assert conflict.data["current_asset"]["metadata_version"] == 2
    assert conflict.data["current_asset"]["name"] == "Shared workflow hg38"
    assert conflict.data["current_asset"]["description"] == ""
    asset = WDLAsset.objects.get(slug=slug)
    assert asset.description == ""
    assert asset.audit_events.filter(action="metadata_update").count() == 1


@pytest.mark.django_db
def test_wdl_writes_require_concurrency_preconditions():
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Protected workflow",
            "filename": "protected.wdl",
            "content": WDL_SOURCE,
        },
        format="json",
    )
    slug = imported.data["slug"]

    revision = client.post(
        f"/api/v1/wdl-assets/{slug}/revisions",
        {"content": WDL_SOURCE.replace("greeting.txt", "protected.txt")},
        format="json",
    )
    metadata = client.patch(
        f"/api/v1/wdl-assets/{slug}",
        {"description": "missing precondition"},
        format="json",
    )

    assert revision.status_code == 428
    assert revision.data["error"]["code"] == "WDL_PRECONDITION_REQUIRED"
    assert metadata.status_code == 428
    assert metadata.data["error"]["code"] == "WDL_PRECONDITION_REQUIRED"
    assert WDLSourceRevision.objects.filter(asset__slug=slug).count() == 1


@pytest.mark.django_db
def test_tag_pool_reuses_names_case_insensitively_and_orders_by_usage():
    client = APIClient()
    first = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "First workflow",
            "filename": "first.wdl",
            "content": WDL_SOURCE,
            "tags": ["hg38", "实体瘤"],
        },
        format="json",
    )
    second = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Second workflow",
            "filename": "second.wdl",
            "content": WDL_SOURCE,
            "tags": ["HG38", "血液肿瘤"],
        },
        format="json",
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert WDLTag.objects.filter(name__iexact="hg38").count() == 1
    assert "hg38" in second.data["tags"]

    tags = client.get("/api/v1/wdl-assets/tags")
    assert tags.status_code == 200
    assert tags.data["results"][0]["name"] == "hg38"
    assert tags.data["results"][0]["asset_count"] == 2
    assert isinstance(tags.data["results"][0]["id"], int)

    reused = client.post(
        "/api/v1/wdl-assets/tags",
        {"name": "HG38"},
        format="json",
    )
    assert reused.status_code == 200
    assert reused.data["name"] == "hg38"
    assert reused.data["asset_count"] == 2
    assert isinstance(reused.data["id"], int)
    assert WDLTag.objects.filter(name__iexact="hg38").count() == 1
    with pytest.raises(IntegrityError), transaction.atomic():
        WDLTag.objects.create(name="HG38")


@pytest.mark.django_db
def test_tag_pool_create_recovers_from_a_concurrent_unique_conflict(monkeypatch):
    client = APIClient()
    canonical = WDLTag.objects.create(name="hg38")

    class MissingTag:
        @staticmethod
        def first():
            return None

    monkeypatch.setattr(WDLTag.objects, "filter", lambda **kwargs: MissingTag())
    monkeypatch.setattr(
        WDLTag.objects,
        "create",
        lambda **kwargs: (_ for _ in ()).throw(IntegrityError("concurrent tag")),
    )
    monkeypatch.setattr(WDLTag.objects, "get", lambda **kwargs: canonical)

    response = client.post(
        "/api/v1/wdl-assets/tags",
        {"name": "HG38"},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["id"] == canonical.id
    assert response.data["name"] == "hg38"


@pytest.mark.django_db
def test_used_tag_can_be_renamed_and_updates_assets_with_audit_history():
    client = APIClient()
    for name in ("First workflow", "Second workflow"):
        response = client.post(
            "/api/v1/wdl-assets",
            {
                "name": name,
                "filename": f"{name.casefold().replace(' ', '-')}.wdl",
                "content": WDL_SOURCE,
                "tags": ["hg38"],
            },
            format="json",
        )
        assert response.status_code == 201

    tag = WDLTag.objects.get(name="hg38")
    renamed = client.patch(
        f"/api/v1/wdl-assets/tags/{tag.id}",
        {"name": "GRCh38"},
        format="json",
    )

    assert renamed.status_code == 200
    assert renamed.data == {
        "id": tag.id,
        "name": "GRCh38",
        "asset_count": 2,
    }
    assert WDLTag.objects.filter(name="GRCh38").exists()
    for asset in WDLAsset.objects.prefetch_related("tags"):
        assert [item.name for item in asset.tags.all()] == ["GRCh38"]
        assert asset.metadata_version == 2
        event = asset.audit_events.first()
        assert event.note == "重命名标签 hg38 → GRCh38"
        assert event.changes["tags"] == {
            "before": ["hg38"],
            "after": ["GRCh38"],
        }


@pytest.mark.django_db
def test_tag_rename_rejects_existing_name_and_used_tag_cannot_be_deleted():
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Workflow",
            "filename": "workflow.wdl",
            "content": WDL_SOURCE,
            "tags": ["hg38"],
        },
        format="json",
    )
    assert imported.status_code == 201
    existing = WDLTag.objects.create(name="hg19")
    used = WDLTag.objects.get(name="hg38")

    conflict = client.patch(
        f"/api/v1/wdl-assets/tags/{used.id}",
        {"name": existing.name.upper()},
        format="json",
    )
    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "WDL_TAG_CONFLICT"
    assert WDLTag.objects.get(id=used.id).name == "hg38"

    blocked = client.delete(f"/api/v1/wdl-assets/tags/{used.id}")
    assert blocked.status_code == 409
    assert blocked.data["error"]["code"] == "WDL_TAG_IN_USE"
    assert blocked.data["asset_count"] == 1
    assert WDLTag.objects.filter(id=used.id).exists()


@pytest.mark.django_db
def test_tag_rename_maps_a_concurrent_unique_conflict_to_409(monkeypatch):
    client = APIClient()
    tag = WDLTag.objects.create(name="hg19")
    original_save = WDLTag.save

    def conflicting_save(instance, *args, **kwargs):
        if instance.id == tag.id and instance.name == "GRCh37":
            raise IntegrityError("concurrent rename")
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(WDLTag, "save", conflicting_save)
    response = client.patch(
        f"/api/v1/wdl-assets/tags/{tag.id}",
        {"name": "GRCh37"},
        format="json",
    )

    assert response.status_code == 409
    assert response.data["error"]["code"] == "WDL_TAG_CONFLICT"
    tag.refresh_from_db()
    assert tag.name == "hg19"


@pytest.mark.django_db
def test_unused_tag_can_be_deleted():
    client = APIClient()
    created = client.post(
        "/api/v1/wdl-assets/tags",
        {"name": "待清理"},
        format="json",
    )
    assert created.status_code == 201

    deleted = client.delete(f"/api/v1/wdl-assets/tags/{created.data['id']}")

    assert deleted.status_code == 204
    assert not WDLTag.objects.filter(id=created.data["id"]).exists()


@pytest.mark.django_db
def test_invalid_historical_wdl_can_be_imported_but_cannot_be_formatted():
    client = APIClient()
    imported = client.post(
        "/api/v1/wdl-assets",
        {
            "name": "Needs repair",
            "filename": "broken.wdl",
            "content": "version 1.0\nworkflow broken {\n",
        },
        format="json",
    )

    assert imported.status_code == 201
    assert imported.data["current_revision"]["analysis"]["status"] == "invalid"
    slug = imported.data["slug"]

    formatted = client.post(
        f"/api/v1/wdl-assets/{slug}/format",
        {"content": imported.data["current_revision"]["content"]},
        format="json",
    )
    assert formatted.status_code == 422
    assert formatted.data["error"]["code"] == "WDL_FORMAT_REQUIRES_VALID_SYNTAX"


@pytest.mark.django_db
def test_wdl_source_revisions_are_immutable():
    asset = WDLAsset.objects.create(
        slug="immutable",
        name="Immutable",
        source_filename="immutable.wdl",
    )
    revision = WDLSourceRevision.objects.create(
        asset=asset,
        version=1,
        operation="import",
        content=WDL_SOURCE,
        digest="sha256:test",
    )

    revision.note = "changed"
    with pytest.raises(ValidationError):
        revision.save()

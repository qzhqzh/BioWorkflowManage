import json
from copy import deepcopy
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from workflows.models import ToolDocument, ToolVersion


ROOT = Path(__file__).resolve().parents[2]


def fastp_spec():
    return json.loads(
        (ROOT / "examples" / "phase1-fastp" / "tool-fastp.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.mark.django_db
def test_invalid_tool_draft_is_saved_with_diagnostics():
    client = APIClient()
    response = client.put(
        "/api/v1/tools/new_tool/drafts",
        {"tool_spec": {"id": "wrong_id", "display_name": "Incomplete"}},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["validation"]["status"] == "invalid"
    assert response.data["draft_spec"]["display_name"] == "Incomplete"
    assert "TOOL_ID_MISMATCH" in {
        item["code"] for item in response.data["validation"]["diagnostics"]
    }
    assert ToolDocument.objects.filter(tool_id="new_tool").exists()


@pytest.mark.django_db
def test_valid_draft_can_be_published_and_version_is_immutable():
    client = APIClient()
    spec = fastp_spec()
    saved = client.post(
        "/api/v1/tools/fastp/drafts", {"tool_spec": spec}, format="json"
    )
    assert saved.status_code == 200
    assert saved.data["validation"]["status"] == "valid"

    missing = client.post("/api/v1/tools/fastp/publish", {}, format="json")
    assert missing.status_code == 428

    publish_body = {
        "base_draft_version": saved.data["draft_version"],
        "base_draft_digest": saved.data["draft_digest"],
    }
    published = client.post(
        "/api/v1/tools/fastp/publish", publish_body, format="json"
    )
    assert published.status_code == 201
    assert published.data["version"] == "0.23.4"

    same = client.post(
        "/api/v1/tools/fastp/publish", publish_body, format="json"
    )
    assert same.status_code == 200

    changed = deepcopy(spec)
    changed["description"] = "Changed without bumping the version."
    updated = client.put(
        "/api/v1/tools/fastp/drafts",
        {
            "tool_spec": changed,
            "base_draft_version": saved.data["draft_version"],
            "base_draft_digest": saved.data["draft_digest"],
        },
        format="json",
    )
    assert updated.status_code == 200
    conflict = client.post(
        "/api/v1/tools/fastp/publish",
        {
            "base_draft_version": updated.data["draft_version"],
            "base_draft_digest": updated.data["draft_digest"],
        },
        format="json",
    )
    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "TOOL_VERSION_IMMUTABLE"
    assert ToolVersion.objects.get(tool_id="fastp").tool_spec == spec


@pytest.mark.django_db
def test_tool_publish_rejects_stale_draft_precondition():
    client = APIClient()
    spec = fastp_spec()
    created = client.put(
        "/api/v1/tools/fastp/drafts", {"tool_spec": spec}, format="json"
    )
    changed = deepcopy(spec)
    changed["description"] = "updated by another editor"
    updated = client.put(
        "/api/v1/tools/fastp/drafts",
        {
            "tool_spec": changed,
            "base_draft_version": created.data["draft_version"],
            "base_draft_digest": created.data["draft_digest"],
        },
        format="json",
    )
    assert updated.status_code == 200

    conflict = client.post(
        "/api/v1/tools/fastp/publish",
        {
            "base_draft_version": created.data["draft_version"],
            "base_draft_digest": created.data["draft_digest"],
        },
        format="json",
    )

    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "TOOL_DRAFT_CONFLICT"
    assert ToolVersion.objects.count() == 0


@pytest.mark.django_db
def test_existing_tool_draft_requires_current_version_and_digest():
    client = APIClient()
    spec = fastp_spec()
    created = client.put(
        "/api/v1/tools/fastp/drafts", {"tool_spec": spec}, format="json"
    )
    assert created.status_code == 200

    missing = client.put(
        "/api/v1/tools/fastp/drafts", {"tool_spec": spec}, format="json"
    )
    assert missing.status_code == 428

    changed = deepcopy(spec)
    changed["description"] = "first editor"
    saved = client.put(
        "/api/v1/tools/fastp/drafts",
        {
            "tool_spec": changed,
            "base_draft_version": created.data["draft_version"],
            "base_draft_digest": created.data["draft_digest"],
        },
        format="json",
    )
    assert saved.status_code == 200

    stale = deepcopy(spec)
    stale["description"] = "stale editor"
    conflict = client.put(
        "/api/v1/tools/fastp/drafts",
        {
            "tool_spec": stale,
            "base_draft_version": created.data["draft_version"],
            "base_draft_digest": created.data["draft_digest"],
        },
        format="json",
    )
    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "TOOL_DRAFT_CONFLICT"
    assert (
        ToolDocument.objects.get(tool_id="fastp").draft_spec["description"]
        == "first editor"
    )


@pytest.mark.django_db
def test_tool_list_includes_unpublished_draft():
    client = APIClient()
    client.put(
        "/api/v1/tools/draft_only/drafts",
        {"tool_spec": {"id": "draft_only", "display_name": "Draft only"}},
        format="json",
    )

    response = client.get("/api/v1/tools")

    assert response.status_code == 200
    item = next(
        row for row in response.data["results"] if row["tool_id"] == "draft_only"
    )
    assert item["latest_version"] is None
    assert item["draft_status"] == "invalid"
    assert item["draft_version"] is None
    assert item["draft_digest"].startswith("sha256:")

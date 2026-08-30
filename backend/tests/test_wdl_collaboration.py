from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from workflows.auth_roles import WORKFLOW_MAINTAINER_GROUP
from workflows.models import (
    AnalysisRun,
    WDLAuditEvent,
    WDLAssetRelease,
    WDLReleaseCheck,
    WDLAsset,
    WDLReviewRequest,
    WDLReviewThread,
    WDLSourceConflict,
)


pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("auth_disabled")]


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
  call hello { input: name = name }
  output {
    File greeting = hello.greeting
  }
}
"""


def _maintainer(username: str):
    user = get_user_model().objects.create_user(username=username)
    group, _ = Group.objects.get_or_create(name=WORKFLOW_MAINTAINER_GROUP)
    user.groups.add(group)
    return user


def _import_asset(client: APIClient, *, name: str = "Shared WDL"):
    response = client.post(
        "/api/v1/wdl-assets",
        {
            "name": name,
            "filename": "workflow.wdl",
            "content": WDL_SOURCE,
        },
        format="json",
    )
    assert response.status_code == 201
    return response.data


def test_review_request_requires_version_precondition_and_is_audited():
    requester = _maintainer("requester")
    reviewer = _maintainer("reviewer")
    client = APIClient()
    client.force_authenticate(requester)
    asset = _import_asset(client)

    requested = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/reviews",
        {
            "revision": 1,
            "assignee": reviewer.username,
            "note": "请检查工具包版本和输出。",
        },
        format="json",
    )

    assert requested.status_code == 201
    assert requested.data["status"] == "pending"
    assert requested.data["assignee"] == reviewer.username
    assert WDLAuditEvent.objects.filter(action="review_requested").exists()

    client.force_authenticate(reviewer)
    missing_precondition = client.patch(
        f"/api/v1/wdl-assets/{asset['slug']}/reviews/{requested.data['id']}",
        {"action": "approve", "conclusion": "可以发布。"},
        format="json",
    )
    assert missing_precondition.status_code == 428

    approved = client.patch(
        f"/api/v1/wdl-assets/{asset['slug']}/reviews/{requested.data['id']}",
        {
            "action": "approve",
            "conclusion": "工具包版本已固定，可以发布。",
            "base_review_version": 1,
        },
        format="json",
    )
    assert approved.status_code == 200
    assert approved.data["status"] == "approved"
    assert approved.data["version"] == 2
    assert approved.data["concluded_by"] == reviewer.username

    stale = client.patch(
        f"/api/v1/wdl-assets/{asset['slug']}/reviews/{requested.data['id']}",
        {
            "action": "request_changes",
            "conclusion": "旧页面提交。",
            "base_review_version": 1,
        },
        format="json",
    )
    assert stale.status_code == 409
    assert stale.data["current_review"]["status"] == "approved"
    assert WDLReviewRequest.objects.get().version == 2
    assert WDLAuditEvent.objects.filter(action="review_approved").exists()


def test_only_assignee_or_admin_can_conclude_review():
    requester = _maintainer("review-requester")
    reviewer = _maintainer("assigned-reviewer")
    bystander = _maintainer("other-reviewer")
    client = APIClient()
    client.force_authenticate(requester)
    asset = _import_asset(client, name="Protected Review")
    requested = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/reviews",
        {"revision": 1, "assignee": reviewer.username, "note": "请评审"},
        format="json",
    )
    assert requested.status_code == 201

    client.force_authenticate(bystander)
    forbidden = client.patch(
        f"/api/v1/wdl-assets/{asset['slug']}/reviews/{requested.data['id']}",
        {
            "action": "approve",
            "conclusion": "越权通过",
            "base_review_version": 1,
        },
        format="json",
    )

    assert forbidden.status_code == 403
    assert WDLReviewRequest.objects.get().status == WDLReviewRequest.Status.PENDING


def test_line_discussion_is_anchored_append_only_and_stale_after_new_revision():
    author = _maintainer("author")
    client = APIClient()
    client.force_authenticate(author)
    asset = _import_asset(client, name="Commented WDL")

    created = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/review-threads",
        {
            "revision": 1,
            "file_path": "workflow.wdl",
            "line": 8,
            "body": "这里需要说明输出文件的命名约定。",
        },
        format="json",
    )
    assert created.status_code == 201
    assert created.data["line"] == 8
    assert created.data["comments"][0]["body"].startswith("这里需要")

    reply = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/review-threads/{created.data['id']}/comments",
        {"body": "已确认由下游 collect 使用。"},
        format="json",
    )
    assert reply.status_code == 201
    assert WDLReviewThread.objects.get().comments.count() == 2

    resolved = client.patch(
        f"/api/v1/wdl-assets/{asset['slug']}/review-threads/{created.data['id']}",
        {"action": "resolve", "base_thread_version": 1},
        format="json",
    )
    assert resolved.status_code == 200
    assert resolved.data["status"] == "resolved"
    assert resolved.data["version"] == 2
    reply_after_resolve = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/review-threads/{created.data['id']}/comments",
        {"body": "绕过重新打开直接回复。"},
        format="json",
    )
    assert reply_after_resolve.status_code == 409
    assert reply_after_resolve.data["error"]["code"] == "WDL_REVIEW_THREAD_RESOLVED"

    revision = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/revisions",
        {
            "content": WDL_SOURCE.replace("greeting.txt", "greeting-v2.txt"),
            "operation": "edit",
            "note": "调整输出名称",
            "base_version": 1,
            "base_digest": asset["current_revision"]["digest"],
        },
        format="json",
    )
    assert revision.status_code == 201

    historical = client.get(
        f"/api/v1/wdl-assets/{asset['slug']}/collaboration",
        {"revision": 1},
    )
    assert historical.status_code == 200
    assert historical.data["latest_revision"] == 2
    assert historical.data["threads"][0]["stale"] is True
    assert historical.data["threads"][0]["comments"][1]["body"].startswith("已确认")
    assert WDLAuditEvent.objects.filter(action="review_thread_resolve").exists()


def test_invalid_comment_anchor_is_rejected():
    maintainer = _maintainer("anchor-reviewer")
    client = APIClient()
    client.force_authenticate(maintainer)
    asset = _import_asset(client, name="Anchored WDL")

    response = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/review-threads",
        {
            "revision": 1,
            "file_path": "missing.wdl",
            "line": 999,
            "body": "无效锚点",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "WDL_REVIEW_ANCHOR_INVALID"
    assert WDLReviewThread.objects.count() == 0


def test_source_conflict_enters_my_queue_and_successful_save_resolves_it():
    editor = _maintainer("source-editor")
    reviewer = _maintainer("source-reviewer")
    client = APIClient()
    client.force_authenticate(editor)
    asset = _import_asset(client, name="Conflict WDL")
    base = asset["current_revision"]

    saved = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/revisions",
        {
            "content": WDL_SOURCE.replace("echo \"~{name}\"", "echo \"editor ~{name}\""),
            "operation": "edit",
            "note": "编辑者更新",
            "base_version": base["version"],
            "base_digest": base["digest"],
        },
        format="json",
    )
    assert saved.status_code == 201

    client.force_authenticate(reviewer)
    conflict = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/revisions",
        {
            "content": WDL_SOURCE.replace("greeting.txt", "reviewer.txt"),
            "operation": "edit",
            "note": "评审者旧草稿",
            "base_version": base["version"],
            "base_digest": base["digest"],
        },
        format="json",
    )
    assert conflict.status_code == 409
    assert WDLSourceConflict.objects.filter(
        actor=reviewer.username,
        resolved_at__isnull=True,
    ).count() == 1

    reviewer.username = "source-reviewer-renamed"
    reviewer.save(update_fields=["username"])

    queue = client.get("/api/v1/wdl-assets", {"work_queue": "mine"})
    assert queue.status_code == 200
    assert [item["slug"] for item in queue.data["results"]] == [asset["slug"]]
    assert queue.data["results"][0]["attention"]["conflicts"] == 1
    collaboration = client.get(
        f"/api/v1/wdl-assets/{asset['slug']}/collaboration",
        {"revision": saved.data["version"]},
    )
    assert collaboration.data["attention"]["open_conflicts"] == 1

    merged = client.post(
        f"/api/v1/wdl-assets/{asset['slug']}/revisions",
        {
            "content": saved.data["content"].replace("greeting.txt", "reviewer.txt"),
            "operation": "edit",
            "note": "合并评审者修改",
            "base_version": saved.data["version"],
            "base_digest": saved.data["digest"],
        },
        format="json",
    )
    assert merged.status_code == 201
    assert not WDLSourceConflict.objects.filter(resolved_at__isnull=True).exists()
    assert client.get("/api/v1/wdl-assets", {"work_queue": "mine"}).data["results"] == []
    assert WDLAuditEvent.objects.filter(action="source_conflict_resolved").exists()


def test_release_check_revalidates_evidence_before_immutable_release():
    requester = _maintainer("release-owner")
    reviewer = _maintainer("release-reviewer")
    client = APIClient()
    client.force_authenticate(requester)
    payload = _import_asset(client, name="Stable WDL")
    asset = WDLAsset.objects.get(slug=payload["slug"])
    revision = asset.source_revisions.first()

    review = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/reviews",
        {"revision": 1, "assignee": reviewer.username, "note": "发布前评审"},
        format="json",
    )
    assert review.status_code == 201
    client.force_authenticate(reviewer)
    approved = client.patch(
        f"/api/v1/wdl-assets/{asset.slug}/reviews/{review.data['id']}",
        {
            "action": "approve",
            "conclusion": "检查通过",
            "base_review_version": 1,
        },
        format="json",
    )
    assert approved.status_code == 200
    AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="greeting",
        sample_id="tiny-sample",
        status=AnalysisRun.Status.SUCCEEDED,
        request_payload={
            "input_manifest": {
                "files": [{"path": "tiny_R1.fastq.gz", "size": 1024}]
            }
        },
    )

    checked = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/release-checks",
        {"revision": 1},
        format="json",
    )
    assert checked.status_code == 201
    assert checked.data["status"] == "passed"
    assert all(item["passed"] for item in checked.data["checks"])

    thread = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/review-threads",
        {
            "revision": 1,
            "file_path": "workflow.wdl",
            "line": 1,
            "body": "检查后新增的发布阻断问题",
        },
        format="json",
    )
    assert thread.status_code == 201
    stale_release = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/releases",
        {
            "release_check_id": checked.data["id"],
            "base_version": 1,
            "base_digest": revision.digest,
            "version": "1.0.0",
        },
        format="json",
    )
    assert stale_release.status_code == 409
    assert stale_release.data["error"]["code"] == "WDL_RELEASE_EVIDENCE_STALE"

    resolved = client.patch(
        f"/api/v1/wdl-assets/{asset.slug}/review-threads/{thread.data['id']}",
        {"action": "resolve", "base_thread_version": 1},
        format="json",
    )
    assert resolved.status_code == 200
    rechecked = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/release-checks",
        {"revision": 1},
        format="json",
    )
    follow_up = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/reviews",
        {"revision": 1, "assignee": requester.username, "note": "补充复核"},
        format="json",
    )
    client.force_authenticate(requester)
    client.patch(
        f"/api/v1/wdl-assets/{asset.slug}/reviews/{follow_up.data['id']}",
        {
            "action": "approve",
            "conclusion": "补充复核通过",
            "base_review_version": 1,
        },
        format="json",
    )
    changed_evidence = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/releases",
        {
            "release_check_id": rechecked.data["id"],
            "base_version": 1,
            "base_digest": revision.digest,
            "version": "1.0.0",
        },
        format="json",
    )
    assert changed_evidence.status_code == 409
    assert changed_evidence.data["error"]["code"] == "WDL_RELEASE_EVIDENCE_STALE"

    final_check = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/release-checks",
        {"revision": 1},
        format="json",
    )
    released = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/releases",
        {
            "release_check_id": final_check.data["id"],
            "base_version": 1,
            "base_digest": revision.digest,
            "version": "1.0.0",
            "note": "稳定基线",
        },
        format="json",
    )

    assert released.status_code == 201
    assert released.data["version"] == "1.0.0"
    assert WDLAssetRelease.objects.get().revision == revision
    assert WDLReleaseCheck.objects.count() == 3
    assert WDLAuditEvent.objects.filter(action="released").exists()


def test_release_policy_is_admin_only_and_version_protected():
    maintainer = _maintainer("policy-maintainer")
    admin = get_user_model().objects.create_superuser(username="policy-admin")
    client = APIClient()
    client.force_authenticate(maintainer)
    forbidden = client.patch(
        "/api/v1/wdl-release-policy",
        {
            "base_policy_version": 1,
            "enabled_checks": ["syntax"],
            "max_input_bytes": 10 * 1024 * 1024,
        },
        format="json",
    )
    assert forbidden.status_code == 403

    client.force_authenticate(admin)
    missing = client.patch(
        "/api/v1/wdl-release-policy",
        {"enabled_checks": ["syntax"], "max_input_bytes": 10 * 1024 * 1024},
        format="json",
    )
    assert missing.status_code == 428
    updated = client.patch(
        "/api/v1/wdl-release-policy",
        {
            "base_policy_version": 1,
            "enabled_checks": ["syntax", "small_data_run"],
            "max_input_bytes": 10 * 1024 * 1024,
        },
        format="json",
    )
    assert updated.status_code == 200
    assert updated.data["version"] == 2

    stale = client.patch(
        "/api/v1/wdl-release-policy",
        {
            "base_policy_version": 1,
            "enabled_checks": ["syntax"],
            "max_input_bytes": 10 * 1024 * 1024,
        },
        format="json",
    )
    assert stale.status_code == 409


def test_release_check_rejects_external_imports_without_managed_source():
    owner = _maintainer("external-import-owner")
    reviewer = _maintainer("external-import-reviewer")
    client = APIClient()
    client.force_authenticate(owner)
    payload = _import_asset(client, name="External Import WDL")
    asset = WDLAsset.objects.get(slug=payload["slug"])
    revision = asset.source_revisions.first()
    revision.analysis["imports"] = [
        {"uri": "https://example.invalid/shared.wdl", "status": "external"}
    ]
    revision.analysis.setdefault("package", {})["external_import_count"] = 1
    type(revision).objects.filter(pk=revision.pk).update(analysis=revision.analysis)
    review = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/reviews",
        {"revision": 1, "assignee": reviewer.username},
        format="json",
    )
    client.force_authenticate(reviewer)
    client.patch(
        f"/api/v1/wdl-assets/{asset.slug}/reviews/{review.data['id']}",
        {"action": "approve", "conclusion": "通过", "base_review_version": 1},
        format="json",
    )
    AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name="greeting",
        sample_id="tiny",
        status=AnalysisRun.Status.SUCCEEDED,
        request_payload={"files": [{"path": "tiny.fq.gz", "size": 1024}]},
    )

    response = client.post(
        f"/api/v1/wdl-assets/{asset.slug}/release-checks",
        {"revision": 1},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["status"] == "failed"
    imports = next(item for item in response.data["checks"] if item["key"] == "imports")
    assert imports["passed"] is False
    assert imports["evidence"]["external"] == 1

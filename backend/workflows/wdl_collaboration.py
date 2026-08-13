from __future__ import annotations

import re

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .auth_roles import WORKFLOW_MAINTAINER_GROUP, is_admin
from .models import (
    AnalysisRun,
    WDLAuditEvent,
    WDLAssetRelease,
    WDLAsset,
    WDLReleaseCheck,
    WDLReleasePolicy,
    WDLReviewComment,
    WDLReviewRequest,
    WDLReviewThread,
    WDLSourceRevision,
)
from .request_ids import request_id, with_request_id
from .wdl_assets import REVISION_REFERENCE_PREFETCHES, _revision_files
from .wdl_packages import WDLPackageError
from .wdl_source_references import effective_package_files, reference_specs_for_revision


MAX_REVIEW_TEXT_LENGTH = 10_000
RELEASE_CHECK_KEYS = {
    "syntax",
    "imports",
    "package_pins",
    "approved_review",
    "resolved_threads",
    "small_data_run",
}


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        username = user.get_username()
        if username:
            return username[:256]
    return "local-user"


def _authenticated_user(request):
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    return None


def _error(request_id_value: str, code: str, message: str, response_status: int):
    return with_request_id(
        Response(
            {"error": {"code": code, "message": message}},
            status=response_status,
        ),
        request_id_value,
    )


def _asset(slug: str):
    return WDLAsset.objects.filter(slug=slug).first()


def _eligible_assignees():
    return (
        get_user_model()
        .objects.filter(is_active=True)
        .filter(
            Q(is_superuser=True)
            | Q(is_staff=True)
            | Q(groups__name=WORKFLOW_MAINTAINER_GROUP)
        )
        .distinct()
        .order_by("username")
    )


def _assignee(username: str):
    if not username:
        return None
    return _eligible_assignees().filter(username=username).first()


def _review_payload(review: WDLReviewRequest) -> dict:
    return {
        "id": review.id,
        "revision": review.revision.version,
        "status": review.status,
        "version": review.version,
        "requester": review.requester_name,
        "assignee": review.assignee_name,
        "request_note": review.request_note,
        "conclusion": review.conclusion,
        "concluded_by": review.concluded_by_name,
        "concluded_at": review.concluded_at.isoformat() if review.concluded_at else None,
        "created_at": review.created_at.isoformat(),
        "updated_at": review.updated_at.isoformat(),
    }


def _comment_payload(comment: WDLReviewComment) -> dict:
    return {
        "id": comment.id,
        "author": comment.author_name,
        "body": comment.body,
        "created_at": comment.created_at.isoformat(),
    }


def _thread_payload(thread: WDLReviewThread, *, latest_version: int) -> dict:
    return {
        "id": thread.id,
        "revision": thread.revision.version,
        "file_path": thread.file_path,
        "line": thread.line,
        "status": thread.status,
        "version": thread.version,
        "created_by": thread.created_by_name,
        "resolved_by": thread.resolved_by_name,
        "resolved_at": thread.resolved_at.isoformat() if thread.resolved_at else None,
        "created_at": thread.created_at.isoformat(),
        "updated_at": thread.updated_at.isoformat(),
        "stale": thread.revision.version != latest_version,
        "comments": [_comment_payload(item) for item in thread.comments.all()],
    }


def _revision_for_asset(asset: WDLAsset, raw_version):
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        return None
    if version < 1:
        return None
    return (
        WDLSourceRevision.objects.filter(asset=asset, version=version)
        .prefetch_related("files", *REVISION_REFERENCE_PREFETCHES)
        .first()
    )


def _valid_text(value, *, required: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (required and not text) or len(text) > MAX_REVIEW_TEXT_LENGTH:
        return None
    return text


def _release_policy() -> WDLReleasePolicy:
    policy, _ = WDLReleasePolicy.objects.get_or_create(key="default")
    return policy


def _policy_payload(policy: WDLReleasePolicy) -> dict:
    return {
        "version": policy.version,
        "enabled_checks": policy.enabled_checks,
        "max_input_bytes": policy.max_input_bytes,
        "updated_by": policy.updated_by,
        "updated_at": policy.updated_at.isoformat(),
    }


def _release_check_payload(check: WDLReleaseCheck) -> dict:
    return {
        "id": check.id,
        "revision": check.revision.version,
        "revision_digest": check.revision.digest,
        "status": check.status,
        "policy_version": check.policy_version,
        "policy_snapshot": check.policy_snapshot,
        "checks": check.checks,
        "analysis_run_id": str(check.analysis_run_id) if check.analysis_run_id else None,
        "requested_by": check.requested_by,
        "created_at": check.created_at.isoformat(),
    }


def _release_payload(release: WDLAssetRelease) -> dict:
    return {
        "id": release.id,
        "version": release.version,
        "revision": release.revision.version,
        "revision_digest": release.revision.digest,
        "release_check_id": release.release_check_id,
        "note": release.note,
        "actor": release.actor,
        "created_at": release.created_at.isoformat(),
    }


def _manifest_input_bytes(value) -> int:
    total = 0
    if isinstance(value, dict):
        if isinstance(value.get("size"), int) and (
            value.get("relative_path") or value.get("path")
        ):
            total += max(0, value["size"])
        else:
            total += sum(_manifest_input_bytes(item) for item in value.values())
    elif isinstance(value, list):
        total += sum(_manifest_input_bytes(item) for item in value)
    return total


def _evaluate_release_checks(
    revision: WDLSourceRevision,
    policy: WDLReleasePolicy,
    *,
    analysis_run_id=None,
) -> tuple[list[dict], AnalysisRun | None]:
    enabled = set(policy.enabled_checks)
    analysis = revision.analysis or {}
    diagnostics = analysis.get("diagnostics", [])
    imports = analysis.get("imports", [])
    package = analysis.get("package", {})
    package_references = list(
        revision.package_references.select_related("package_version").all()
    )
    latest_review = revision.review_requests.order_by("-created_at", "-id").first()
    open_thread_count = revision.review_threads.filter(
        status=WDLReviewThread.Status.OPEN
    ).count()
    run_query = AnalysisRun.objects.filter(
        revision=revision,
        run_kind=AnalysisRun.Kind.WORKFLOW,
        status=AnalysisRun.Status.SUCCEEDED,
    )
    run = None
    if analysis_run_id:
        try:
            run = run_query.filter(pk=analysis_run_id).first()
        except (ValidationError, ValueError):
            run = None
    else:
        run = run_query.order_by("-finished_at", "-created_at").first()
    input_bytes = _manifest_input_bytes(run.request_payload if run else {})

    values = {
        "syntax": (
            analysis.get("status") == "valid"
            and not any(item.get("severity") == "error" for item in diagnostics),
            "语法与静态检查通过",
            {"error_count": sum(item.get("severity") == "error" for item in diagnostics)},
        ),
        "imports": (
            int(package.get("missing_import_count", 0)) == 0
            and int(package.get("external_import_count", 0)) == 0
            and all(item.get("status", "resolved") == "resolved" for item in imports),
            "imports 均可解析",
            {
                "import_count": len(imports),
                "missing": int(package.get("missing_import_count", 0)),
                "external": int(package.get("external_import_count", 0)),
            },
        ),
        "package_pins": (
            all(
                item.digest == item.package_version.digest
                and bool(item.package_version.version)
                for item in package_references
            ),
            "工具包引用已固定到不可变版本",
            {"package_reference_count": len(package_references)},
        ),
        "approved_review": (
            latest_review is not None
            and latest_review.status == WDLReviewRequest.Status.APPROVED,
            "当前 revision 的最近一次评审已通过",
            {
                "review_id": latest_review.id if latest_review else None,
                "status": latest_review.status if latest_review else None,
            },
        ),
        "resolved_threads": (
            open_thread_count == 0,
            "当前 revision 没有未解决讨论",
            {"open_thread_count": open_thread_count},
        ),
        "small_data_run": (
            run is not None and input_bytes <= policy.max_input_bytes,
            "当前 revision 有成功的小数据运行",
            {
                "analysis_run_id": str(run.id) if run else None,
                "input_bytes": input_bytes,
                "max_input_bytes": policy.max_input_bytes,
            },
        ),
    }
    checks = [
        {"key": key, "passed": values[key][0], "label": values[key][1], "evidence": values[key][2]}
        for key in policy.enabled_checks
        if key in enabled and key in values
    ]
    return checks, run


@api_view(["GET"])
def wdl_asset_collaboration(request, slug: str):
    request_id_value = request_id(request)
    asset = _asset(slug)
    if asset is None:
        return _error(
            request_id_value,
            "WDL_ASSET_NOT_FOUND",
            "WDL asset not found.",
            status.HTTP_404_NOT_FOUND,
        )
    latest = asset.source_revisions.first()
    requested_version = request.query_params.get("revision")
    revision = (
        _revision_for_asset(asset, requested_version)
        if requested_version is not None
        else latest
    )
    if revision is None or latest is None:
        return _error(
            request_id_value,
            "WDL_SOURCE_REVISION_NOT_FOUND",
            "WDL source revision not found.",
            status.HTTP_404_NOT_FOUND,
        )
    reviews = list(
        revision.review_requests.select_related("revision").order_by("-created_at", "-id")
    )
    threads = list(
        revision.review_threads.select_related("revision")
        .prefetch_related("comments")
        .order_by("status", "file_path", "line", "created_at", "id")
    )
    user = _authenticated_user(request)
    username = _actor(request)
    open_conflicts_query = asset.source_conflicts.filter(resolved_at__isnull=True)
    open_conflicts = open_conflicts_query.filter(
        Q(assigned_to=user) if user is not None else Q(actor=username)
    ).count()
    pending_reviews = sum(
        item.status == WDLReviewRequest.Status.PENDING
        and item.assignee_name == username
        for item in asset.review_requests.all()
    )
    policy = _release_policy()
    release_checks = list(
        revision.release_checks.select_related("revision", "analysis_run")[:5]
    )
    releases = list(
        asset.releases.select_related("revision", "release_check")[:5]
    )
    return with_request_id(
        Response(
            {
                "asset": asset.slug,
                "revision": revision.version,
                "latest_revision": latest.version,
                "is_latest": revision.version == latest.version,
                "reviews": [_review_payload(item) for item in reviews],
                "threads": [
                    _thread_payload(item, latest_version=latest.version)
                    for item in threads
                ],
                "assignees": [
                    {"username": item.get_username()}
                    for item in _eligible_assignees()
                ],
                "me": user.get_username() if user else username,
                "attention": {
                    "pending_reviews": pending_reviews,
                    "open_conflicts": open_conflicts,
                    "total": pending_reviews + open_conflicts,
                },
                "governance": {
                    "policy": _policy_payload(policy),
                    "can_manage_policy": bool(user is None or is_admin(user)),
                    "checks": [_release_check_payload(item) for item in release_checks],
                    "releases": [_release_payload(item) for item in releases],
                },
            }
        ),
        request_id_value,
    )


@api_view(["GET", "PATCH"])
def wdl_release_policy(request):
    request_id_value = request_id(request)
    policy = _release_policy()
    if request.method == "GET":
        return with_request_id(Response(_policy_payload(policy)), request_id_value)
    user = _authenticated_user(request)
    if user is not None and not is_admin(user):
        return _error(
            request_id_value,
            "WDL_RELEASE_POLICY_ADMIN_REQUIRED",
            "只有管理员可以修改发布检查模板。",
            status.HTTP_403_FORBIDDEN,
        )
    base_version = request.data.get("base_policy_version")
    if base_version is None:
        return _error(
            request_id_value,
            "WDL_RELEASE_POLICY_PRECONDITION_REQUIRED",
            "base_policy_version is required.",
            status.HTTP_428_PRECONDITION_REQUIRED,
        )
    try:
        base_version = int(base_version)
    except (TypeError, ValueError):
        return _error(request_id_value, "WDL_RELEASE_POLICY_INVALID", "Invalid policy version.", 400)
    enabled_checks = request.data.get("enabled_checks")
    if (
        not isinstance(enabled_checks, list)
        or not enabled_checks
        or len(enabled_checks) != len(set(enabled_checks))
        or any(item not in RELEASE_CHECK_KEYS for item in enabled_checks)
    ):
        return _error(
            request_id_value,
            "WDL_RELEASE_POLICY_INVALID",
            "enabled_checks contains an unsupported release check.",
            400,
        )
    try:
        max_input_bytes = int(request.data.get("max_input_bytes"))
    except (TypeError, ValueError):
        max_input_bytes = 0
    if not 1_048_576 <= max_input_bytes <= 107_374_182_400:
        return _error(
            request_id_value,
            "WDL_RELEASE_POLICY_INVALID",
            "小数据上限必须在 1 MiB 到 100 GiB 之间。",
            400,
        )
    with transaction.atomic():
        locked = WDLReleasePolicy.objects.select_for_update().get(pk=policy.pk)
        if locked.version != base_version:
            return with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WDL_RELEASE_POLICY_CONFLICT",
                            "message": "发布检查模板已更新，请重新载入。",
                        },
                        "current_policy": _policy_payload(locked),
                    },
                    status=status.HTTP_409_CONFLICT,
                ),
                request_id_value,
            )
        locked.enabled_checks = enabled_checks
        locked.max_input_bytes = max_input_bytes
        locked.updated_by = _actor(request)
        locked.version += 1
        locked.save()
    return with_request_id(Response(_policy_payload(locked)), request_id_value)


@api_view(["POST"])
def wdl_release_checks(request, slug: str):
    request_id_value = request_id(request)
    asset = _asset(slug)
    if asset is None:
        return _error(request_id_value, "WDL_ASSET_NOT_FOUND", "WDL asset not found.", 404)
    revision = _revision_for_asset(asset, request.data.get("revision"))
    if revision is None:
        return _error(
            request_id_value,
            "WDL_SOURCE_REVISION_NOT_FOUND",
            "Select a saved WDL revision.",
            404,
        )
    policy = _release_policy()
    checks, run = _evaluate_release_checks(
        revision,
        policy,
        analysis_run_id=request.data.get("analysis_run_id"),
    )
    passed = bool(checks) and all(item["passed"] for item in checks)
    release_check = WDLReleaseCheck.objects.create(
        asset=asset,
        revision=revision,
        analysis_run=run,
        status=(
            WDLReleaseCheck.Status.PASSED
            if passed
            else WDLReleaseCheck.Status.FAILED
        ),
        policy_version=policy.version,
        policy_snapshot=_policy_payload(policy),
        checks=checks,
        requested_by=_actor(request),
    )
    WDLAuditEvent.objects.create(
        asset=asset,
        revision=revision,
        action="release_checked",
        actor=_actor(request),
        changes={
            "release_check_id": release_check.id,
            "status": release_check.status,
            "policy_version": policy.version,
            "analysis_run_id": str(run.id) if run else None,
        },
    )
    return with_request_id(
        Response(
            _release_check_payload(release_check),
            status=status.HTTP_201_CREATED,
        ),
        request_id_value,
    )


@api_view(["POST"])
def wdl_asset_releases(request, slug: str):
    request_id_value = request_id(request)
    if request.data.get("base_version") is None or request.data.get("base_digest") is None:
        return _error(
            request_id_value,
            "WDL_RELEASE_PRECONDITION_REQUIRED",
            "base_version and base_digest are required.",
            status.HTTP_428_PRECONDITION_REQUIRED,
        )
    try:
        base_version = int(request.data.get("base_version"))
        release_check_id = int(request.data.get("release_check_id"))
    except (TypeError, ValueError):
        return _error(request_id_value, "WDL_RELEASE_INVALID", "Invalid release input.", 400)
    base_digest = str(request.data.get("base_digest"))
    release_version = str(request.data.get("version") or f"r{base_version}").strip()
    note = _valid_text(request.data.get("note", ""))
    if (
        note is None
        or not release_version
        or len(release_version) > 64
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release_version) is None
    ):
        return _error(
            request_id_value,
            "WDL_RELEASE_INVALID",
            "发布版本仅支持字母、数字、点、下划线和连字符。",
            400,
        )
    with transaction.atomic():
        asset = WDLAsset.objects.select_for_update().filter(slug=slug).first()
        if asset is None:
            return _error(request_id_value, "WDL_ASSET_NOT_FOUND", "WDL asset not found.", 404)
        revision = asset.source_revisions.first()
        if (
            revision is None
            or revision.version != base_version
            or revision.digest != base_digest
        ):
            return _error(
                request_id_value,
                "WDL_RELEASE_SOURCE_CONFLICT",
                "源码版本已变化，请基于最新 revision 重新执行发布检查。",
                status.HTTP_409_CONFLICT,
            )
        release_check = (
            WDLReleaseCheck.objects.select_related("analysis_run", "revision")
            .filter(pk=release_check_id, asset=asset, revision=revision)
            .first()
        )
        policy = WDLReleasePolicy.objects.select_for_update().get(key="default")
        if (
            release_check is None
            or release_check.status != WDLReleaseCheck.Status.PASSED
            or release_check.policy_version != policy.version
        ):
            return _error(
                request_id_value,
                "WDL_RELEASE_CHECK_REQUIRED",
                "发布检查不存在、未通过或检查模板已变化，请重新检查。",
                409,
            )
        current_checks, current_run = _evaluate_release_checks(
            revision,
            policy,
            analysis_run_id=release_check.analysis_run_id,
        )
        if (
            not current_checks
            or not all(item["passed"] for item in current_checks)
            or current_checks != release_check.checks
        ):
            return _error(
                request_id_value,
                "WDL_RELEASE_EVIDENCE_STALE",
                "发布证据已变化，请处理未通过项后重新检查。",
                409,
            )
        if WDLAssetRelease.objects.filter(
            Q(asset=asset, revision=revision) | Q(asset=asset, version=release_version)
        ).exists():
            return _error(
                request_id_value,
                "WDL_RELEASE_ALREADY_EXISTS",
                "该 revision 或发布版本已经存在。",
                409,
            )
        release = WDLAssetRelease.objects.create(
            asset=asset,
            revision=revision,
            release_check=release_check,
            version=release_version,
            note=note,
            actor=_actor(request),
        )
        WDLAuditEvent.objects.create(
            asset=asset,
            revision=revision,
            action="released",
            actor=_actor(request),
            note=note,
            changes={
                "release_id": release.id,
                "version": release.version,
                "release_check_id": release_check.id,
                "analysis_run_id": str(current_run.id) if current_run else None,
            },
        )
    return with_request_id(
        Response(_release_payload(release), status=status.HTTP_201_CREATED),
        request_id_value,
    )


@api_view(["POST"])
def wdl_review_requests(request, slug: str):
    request_id_value = request_id(request)
    asset = _asset(slug)
    if asset is None:
        return _error(request_id_value, "WDL_ASSET_NOT_FOUND", "WDL asset not found.", 404)
    revision = _revision_for_asset(asset, request.data.get("revision"))
    if revision is None:
        return _error(
            request_id_value,
            "WDL_SOURCE_REVISION_NOT_FOUND",
            "Select a saved WDL revision.",
            404,
        )
    assignee = _assignee(str(request.data.get("assignee") or "").strip())
    if assignee is None:
        return _error(
            request_id_value,
            "WDL_REVIEW_ASSIGNEE_INVALID",
            "请选择有效的 WDL 维护者或管理员。",
            400,
        )
    note = _valid_text(request.data.get("note", ""))
    if note is None:
        return _error(
            request_id_value,
            "WDL_REVIEW_NOTE_INVALID",
            "评审说明不能超过 10,000 个字符。",
            400,
        )
    actor = _actor(request)
    user = _authenticated_user(request)
    try:
        with transaction.atomic():
            WDLAsset.objects.select_for_update().get(pk=asset.pk)
            review = WDLReviewRequest.objects.create(
                asset=asset,
                revision=revision,
                requester=user,
                requester_name=actor,
                assignee=assignee,
                assignee_name=assignee.get_username(),
                request_note=note,
            )
            WDLAuditEvent.objects.create(
                asset=asset,
                revision=revision,
                action="review_requested",
                actor=actor,
                note=note,
                changes={
                    "review_id": review.id,
                    "assignee": assignee.get_username(),
                    "status": WDLReviewRequest.Status.PENDING,
                },
            )
    except IntegrityError:
        return _error(
            request_id_value,
            "WDL_REVIEW_ALREADY_PENDING",
            "该 WDL revision 已有待处理评审，请先完成或取消。",
            409,
        )
    return with_request_id(
        Response(_review_payload(review), status=status.HTTP_201_CREATED),
        request_id_value,
    )


@api_view(["PATCH"])
def wdl_review_request_detail(request, slug: str, review_id: int):
    request_id_value = request_id(request)
    base_version = request.data.get("base_review_version")
    if base_version is None:
        return _error(
            request_id_value,
            "WDL_REVIEW_PRECONDITION_REQUIRED",
            "base_review_version is required.",
            status.HTTP_428_PRECONDITION_REQUIRED,
        )
    try:
        base_version = int(base_version)
    except (TypeError, ValueError):
        return _error(request_id_value, "WDL_REVIEW_INVALID", "Invalid review version.", 400)
    action = str(request.data.get("action") or "").strip()
    if action not in {"approve", "request_changes", "cancel", "reassign"}:
        return _error(request_id_value, "WDL_REVIEW_ACTION_INVALID", "Invalid review action.", 400)
    conclusion = _valid_text(
        request.data.get("conclusion", ""),
        required=action in {"approve", "request_changes"},
    )
    if conclusion is None:
        return _error(
            request_id_value,
            "WDL_REVIEW_CONCLUSION_INVALID",
            "评审结论必填，且不能超过 10,000 个字符。",
            400,
        )
    actor = _actor(request)
    user = _authenticated_user(request)
    with transaction.atomic():
        locked_asset = WDLAsset.objects.select_for_update().filter(slug=slug).first()
        if locked_asset is None:
            return _error(request_id_value, "WDL_ASSET_NOT_FOUND", "WDL asset not found.", 404)
        review = (
            WDLReviewRequest.objects.select_for_update()
            .select_related("revision", "asset")
            .filter(id=review_id, asset__slug=slug)
            .first()
        )
        if review is None:
            return _error(request_id_value, "WDL_REVIEW_NOT_FOUND", "WDL review not found.", 404)
        if review.version != base_version:
            return with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WDL_REVIEW_CONFLICT",
                            "message": "评审状态已更新，请载入最新状态后重试。",
                        },
                        "current_review": _review_payload(review),
                    },
                    status=status.HTTP_409_CONFLICT,
                ),
                request_id_value,
            )
        if review.status != WDLReviewRequest.Status.PENDING:
            return _error(
                request_id_value,
                "WDL_REVIEW_ALREADY_CONCLUDED",
                "该评审已经结束。",
                409,
            )
        if user is not None:
            if action in {"approve", "request_changes"} and (
                review.assignee_id != user.id and not is_admin(user)
            ):
                return _error(
                    request_id_value,
                    "WDL_REVIEW_ASSIGNEE_REQUIRED",
                    "只有被指派的评审人或管理员可以提交评审结论。",
                    status.HTTP_403_FORBIDDEN,
                )
            if action in {"cancel", "reassign"} and (
                review.requester_id != user.id and not is_admin(user)
            ):
                return _error(
                    request_id_value,
                    "WDL_REVIEW_REQUESTER_REQUIRED",
                    "只有评审发起人或管理员可以取消或重新指派。",
                    status.HTTP_403_FORBIDDEN,
                )
        before = _review_payload(review)
        if action == "reassign":
            assignee = _assignee(str(request.data.get("assignee") or "").strip())
            if assignee is None:
                return _error(
                    request_id_value,
                    "WDL_REVIEW_ASSIGNEE_INVALID",
                    "请选择有效的 WDL 维护者或管理员。",
                    400,
                )
            review.assignee = assignee
            review.assignee_name = assignee.get_username()
            audit_action = "review_reassigned"
        else:
            review.status = {
                "approve": WDLReviewRequest.Status.APPROVED,
                "request_changes": WDLReviewRequest.Status.CHANGES_REQUESTED,
                "cancel": WDLReviewRequest.Status.CANCELLED,
            }[action]
            review.conclusion = conclusion
            review.concluded_by = user
            review.concluded_by_name = actor
            review.concluded_at = timezone.now()
            audit_action = f"review_{review.status}"
        review.version += 1
        review.save()
        WDLAuditEvent.objects.create(
            asset=review.asset,
            revision=review.revision,
            action=audit_action,
            actor=actor,
            note=conclusion,
            changes={
                "review_id": review.id,
                "before": before,
                "after": _review_payload(review),
            },
        )
    return with_request_id(Response(_review_payload(review)), request_id_value)


def _effective_file_content(revision: WDLSourceRevision, file_path: str) -> str | None:
    try:
        local_files, _ = _revision_files(revision)
        files, _ = effective_package_files(
            local_files,
            reference_specs_for_revision(revision),
        )
    except WDLPackageError:
        return None
    return files.get(file_path)


@api_view(["POST"])
def wdl_review_threads(request, slug: str):
    request_id_value = request_id(request)
    asset = _asset(slug)
    if asset is None:
        return _error(request_id_value, "WDL_ASSET_NOT_FOUND", "WDL asset not found.", 404)
    revision = _revision_for_asset(asset, request.data.get("revision"))
    if revision is None:
        return _error(request_id_value, "WDL_SOURCE_REVISION_NOT_FOUND", "WDL revision not found.", 404)
    file_path = str(request.data.get("file_path") or "").strip()
    try:
        line = int(request.data.get("line"))
    except (TypeError, ValueError):
        line = 0
    body = _valid_text(request.data.get("body"), required=True)
    content = _effective_file_content(revision, file_path)
    if content is None or line < 1 or line > max(1, len(content.splitlines())):
        return _error(
            request_id_value,
            "WDL_REVIEW_ANCHOR_INVALID",
            "请选择当前 revision 中存在的文件和行号。",
            400,
        )
    if body is None:
        return _error(
            request_id_value,
            "WDL_REVIEW_COMMENT_INVALID",
            "评论必填，且不能超过 10,000 个字符。",
            400,
        )
    actor = _actor(request)
    user = _authenticated_user(request)
    with transaction.atomic():
        WDLAsset.objects.select_for_update().get(pk=asset.pk)
        thread = WDLReviewThread.objects.create(
            asset=asset,
            revision=revision,
            file_path=file_path,
            line=line,
            created_by=user,
            created_by_name=actor,
        )
        WDLReviewComment.objects.create(
            thread=thread,
            author=user,
            author_name=actor,
            body=body,
        )
        WDLAuditEvent.objects.create(
            asset=asset,
            revision=revision,
            action="review_comment_added",
            actor=actor,
            note=body,
            changes={"thread_id": thread.id, "file_path": file_path, "line": line},
        )
    thread = WDLReviewThread.objects.prefetch_related("comments").get(pk=thread.pk)
    latest_version = asset.source_revisions.first().version
    return with_request_id(
        Response(
            _thread_payload(thread, latest_version=latest_version),
            status=status.HTTP_201_CREATED,
        ),
        request_id_value,
    )


@api_view(["POST"])
def wdl_review_thread_comments(request, slug: str, thread_id: int):
    request_id_value = request_id(request)
    body = _valid_text(request.data.get("body"), required=True)
    if body is None:
        return _error(
            request_id_value,
            "WDL_REVIEW_COMMENT_INVALID",
            "评论必填，且不能超过 10,000 个字符。",
            400,
        )
    actor = _actor(request)
    user = _authenticated_user(request)
    with transaction.atomic():
        asset = WDLAsset.objects.select_for_update().filter(slug=slug).first()
        thread = (
            WDLReviewThread.objects.select_for_update()
            .select_related("asset", "revision")
            .filter(id=thread_id, asset=asset)
            .first()
        )
        if thread is None:
            return _error(request_id_value, "WDL_REVIEW_THREAD_NOT_FOUND", "Review thread not found.", 404)
        if thread.status != WDLReviewThread.Status.OPEN:
            return _error(
                request_id_value,
                "WDL_REVIEW_THREAD_RESOLVED",
                "该讨论已解决；如需继续，请先重新打开。",
                409,
            )
        comment = WDLReviewComment.objects.create(
            thread=thread,
            author=user,
            author_name=actor,
            body=body,
        )
        WDLAuditEvent.objects.create(
            asset=thread.asset,
            revision=thread.revision,
            action="review_comment_added",
            actor=actor,
            note=body,
            changes={"thread_id": thread.id, "comment_id": comment.id},
        )
    return with_request_id(
        Response(_comment_payload(comment), status=status.HTTP_201_CREATED),
        request_id_value,
    )


@api_view(["PATCH"])
def wdl_review_thread_detail(request, slug: str, thread_id: int):
    request_id_value = request_id(request)
    base_version = request.data.get("base_thread_version")
    if base_version is None:
        return _error(
            request_id_value,
            "WDL_REVIEW_PRECONDITION_REQUIRED",
            "base_thread_version is required.",
            status.HTTP_428_PRECONDITION_REQUIRED,
        )
    try:
        base_version = int(base_version)
    except (TypeError, ValueError):
        return _error(request_id_value, "WDL_REVIEW_INVALID", "Invalid thread version.", 400)
    action = str(request.data.get("action") or "").strip()
    if action not in {"resolve", "reopen"}:
        return _error(request_id_value, "WDL_REVIEW_ACTION_INVALID", "Invalid thread action.", 400)
    actor = _actor(request)
    user = _authenticated_user(request)
    with transaction.atomic():
        asset = WDLAsset.objects.select_for_update().filter(slug=slug).first()
        thread = (
            WDLReviewThread.objects.select_for_update()
            .select_related("asset", "revision")
            .prefetch_related("comments")
            .filter(id=thread_id, asset=asset)
            .first()
        )
        if thread is None:
            return _error(request_id_value, "WDL_REVIEW_THREAD_NOT_FOUND", "Review thread not found.", 404)
        if thread.version != base_version:
            latest_version = thread.asset.source_revisions.first().version
            return with_request_id(
                Response(
                    {
                        "error": {
                            "code": "WDL_REVIEW_THREAD_CONFLICT",
                            "message": "讨论状态已更新，请载入最新版后重试。",
                        },
                        "current_thread": _thread_payload(
                            thread,
                            latest_version=latest_version,
                        ),
                    },
                    status=status.HTTP_409_CONFLICT,
                ),
                request_id_value,
            )
        target_status = (
            WDLReviewThread.Status.RESOLVED
            if action == "resolve"
            else WDLReviewThread.Status.OPEN
        )
        if thread.status != target_status:
            thread.status = target_status
            thread.version += 1
            if target_status == WDLReviewThread.Status.RESOLVED:
                thread.resolved_by = user
                thread.resolved_by_name = actor
                thread.resolved_at = timezone.now()
            else:
                thread.resolved_by = None
                thread.resolved_by_name = ""
                thread.resolved_at = None
            thread.save()
            WDLAuditEvent.objects.create(
                asset=thread.asset,
                revision=thread.revision,
                action=f"review_thread_{action}",
                actor=actor,
                changes={
                    "thread_id": thread.id,
                    "file_path": thread.file_path,
                    "line": thread.line,
                    "status": target_status,
                },
            )
    latest_version = thread.asset.source_revisions.first().version
    return with_request_id(
        Response(_thread_payload(thread, latest_version=latest_version)),
        request_id_value,
    )

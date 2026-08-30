from __future__ import annotations

import json
import os
import re
import stat as stat_module
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import IntegerField, Q
from django.db.models.functions import Cast
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from .analysis_runs import (
    AnalysisInputError,
    _catalog_resource_manifest,
    _canonical_digest,
    _compile_published_workflow,
    _output_payload,
    _requirements,
    _run_timing_payload,
    _workflow_graph_summary,
    _workflow_interface,
    load_database_catalog,
)
from .analysis_runtime import (
    _available_memory_bytes,
    _failure_metadata,
    _verify_run_resource_manifests,
)
from .analysis_products import (
    AnalysisProductError,
    analysis_product_version_is_current,
    normalize_contract_version,
)
from .auth_permissions import (
    IntegrationScopePermission,
    ServicePrincipal,
    require_service_scopes,
    require_service_scopes_by_method,
)
from .integration_outputs import (
    _file_identity,
    _open_regular_readonly,
    _read_gzip_text_lines,
    GzipProbeLineLimitError,
    ResourceSnapshotBudget,
    ResourceSnapshotBudgetError,
    ResourceSnapshotChangedError,
    open_verified_output,
    output_manifest_file_item_is_verified,
    output_manifest_has_integrity_v2,
    public_output_manifest,
)
from .models import (
    AnalysisProductVersion,
    AnalysisRun,
    AnalysisRunEvent,
    ServiceAccount,
    SoftwareAsset,
    ToolVersion,
    WorkflowDocument,
    WorkflowVersion,
)
from .request_ids import request_id, with_request_id
from .tool_runs import _safe_identifier, _tool_test_bundle, _validate_constraints


EXTERNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TERMINAL_STATUSES = {
    AnalysisRun.Status.SUCCEEDED,
    AnalysisRun.Status.FAILED,
    AnalysisRun.Status.CANCELED,
}
ROOT_ALIASES = {
    "rawdata": ("ANALYSIS_RAWDATA_ROOT", "ANALYSIS_RAWDATA_EXECUTION_ROOT"),
    "database": ("ANALYSIS_DATABASE_ROOT", "ANALYSIS_DATABASE_EXECUTION_ROOT"),
}
PROHIBITED_METADATA_KEYS = {
    "patient_name",
    "patient",
    "hospital",
    "doctor",
    "患者姓名",
    "医院",
    "医生",
}
PROHIBITED_METADATA_KEYS_NORMALIZED = {
    re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())
    for value in PROHIBITED_METADATA_KEYS
}


class IntegrationAPIError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "validation",
        retryable: bool = False,
        details: Any = None,
        http_status: int = status.HTTP_400_BAD_REQUEST,
    ):
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.details = details or {}
        self.http_status = http_status


def _error_response(request, error: IntegrationAPIError) -> Response:
    value = request_id(request)
    return with_request_id(
        Response(
            {
                "error": {
                    "code": error.code,
                    "category": error.category,
                    "message": _public_text(error),
                    "retryable": error.retryable,
                    "details": _public_value(error.details),
                    "request_id": value,
                }
            },
            status=error.http_status,
        ),
        value,
    )


def _service_account(request) -> ServiceAccount | None:
    user = getattr(request, "user", None)
    if isinstance(user, ServicePrincipal):
        return user.service_account
    return None


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_username()
    return "integration"


def _visible_runs(request):
    queryset = AnalysisRun.objects.select_related(
        "service_account",
        "analysis_product_version",
        "analysis_product_version__product",
        "workflow_version",
        "workflow_version__workflow",
        "tool_version",
        "retry_of",
    ).filter(service_account__isnull=False)
    account = _service_account(request)
    return queryset.filter(service_account=account) if account else queryset


def _request_service_account(request, external_ref: dict[str, Any]) -> ServiceAccount:
    authenticated = _service_account(request)
    requested_client = str(external_ref.get("client_id") or "").strip()
    if authenticated is not None:
        if requested_client and requested_client != authenticated.client_id:
            raise IntegrationAPIError(
                "EXTERNAL_CLIENT_MISMATCH",
                "external_ref.client_id 与当前 Service Account 不一致。",
                category="authorization",
                http_status=status.HTTP_403_FORBIDDEN,
            )
        return authenticated
    if not requested_client:
        raise IntegrationAPIError(
            "EXTERNAL_CLIENT_REQUIRED",
            "管理员代投递时必须指定 external_ref.client_id。",
        )
    account = ServiceAccount.objects.filter(
        client_id=requested_client,
        is_active=True,
    ).first()
    if account is None:
        raise IntegrationAPIError(
            "EXTERNAL_CLIENT_NOT_FOUND",
            "Service Account 不存在或已停用。",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return account


def _external_ref(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise IntegrationAPIError("EXTERNAL_REF_INVALID", "external_ref 必须是 JSON object。")
    run_id = str(value.get("external_run_id") or "").strip()
    analysis_id = str(value.get("external_analysis_id") or "").strip()
    if not EXTERNAL_ID_PATTERN.fullmatch(run_id):
        raise IntegrationAPIError(
            "EXTERNAL_RUN_ID_INVALID",
            "external_run_id 不能为空，且只能包含字母、数字、点、冒号、下划线和连字符。",
        )
    if analysis_id and not EXTERNAL_ID_PATTERN.fullmatch(analysis_id):
        raise IntegrationAPIError(
            "EXTERNAL_ANALYSIS_ID_INVALID",
            "external_analysis_id 格式无效。",
        )
    return {
        "client_id": str(value.get("client_id") or "").strip(),
        "external_run_id": run_id,
        "external_analysis_id": analysis_id,
    }


def _idempotency_key(request) -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if not IDEMPOTENCY_PATTERN.fullmatch(value):
        raise IntegrationAPIError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key 不能为空，且必须是稳定的外部任务标识。",
        )
    return value


def _clinical_identity_paths(value: Any, *, path: str = "") -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", key.casefold())
            if normalized in PROHIBITED_METADATA_KEYS_NORMALIZED:
                matches.append(child_path)
            matches.extend(_clinical_identity_paths(item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            matches.extend(_clinical_identity_paths(item, path=child_path))
    return matches


def _validate_metadata(value: Any, *, field: str = "metadata") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise IntegrationAPIError("METADATA_INVALID", "metadata 必须是 JSON object。")
    prohibited = sorted(_clinical_identity_paths(value, path=field))
    if prohibited:
        raise IntegrationAPIError(
            "METADATA_CONTAINS_CLINICAL_IDENTITY",
            "执行元数据不得包含患者、医院或医生身份信息。",
            details={"paths": prohibited},
        )
    return value


def _output_semantic_ready(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    semantic_type = str(item.get("semantic_type") or "").strip()
    return bool(semantic_type and semantic_type != "core.output.unknown")


def _workflow_version_payload(version: WorkflowVersion) -> dict[str, Any]:
    snapshot_error = ""
    try:
        _compile_published_workflow(version)
    except Exception as error:
        snapshot_error = str(error)
    ready = not snapshot_error
    outputs = version.interface_contract.get("outputs")
    output_contract_ready = bool(
        isinstance(outputs, list)
        and outputs
        and all(_output_semantic_ready(item) for item in outputs)
    )
    blockers = []
    if not ready:
        blockers.append(snapshot_error or "缺少固定编译产物。")
    if not output_contract_ready:
        blockers.append("缺少语义化输出契约。")
    return {
        "id": version.pk,
        "slug": version.workflow.slug,
        "name": version.name,
        "version": version.version,
        "kind": version.kind,
        "semantic_digest": version.semantic_digest,
        "source_digest": version.compiled_digest,
        "compiler_profile": version.compiler_profile,
        "interface": version.interface_contract,
        "graph_summary": _workflow_graph_summary(version),
        "ready": ready and output_contract_ready,
        "blockers": blockers,
        "created_at": version.created_at,
    }


def _analysis_product_reference(item: AnalysisProductVersion) -> dict[str, Any]:
    return {
        "analysis_code": item.product.code,
        "contract_version": item.contract_version,
        "contract_digest": item.contract_digest,
    }


def _analysis_product_version_payload(
    item: AnalysisProductVersion,
) -> dict[str, Any]:
    workflow = _workflow_version_payload(item.workflow_version)
    snapshot_current = analysis_product_version_is_current(item)
    interface_contract = (
        item.interface_contract if isinstance(item.interface_contract, dict) else {}
    )
    blockers = list(workflow["blockers"])
    if not item.product.is_active:
        blockers.insert(0, "分析产品已停用。")
    if not snapshot_current:
        blockers.append("分析产品契约与固定 WorkflowVersion 快照不一致。")
    return {
        **_analysis_product_reference(item),
        "name": item.product.name,
        "description": item.product.description,
        "active": item.product.is_active,
        "workflow": {
            "source_type": "workflow_version",
            "version_id": item.workflow_version_id,
            "slug": item.workflow_version.workflow.slug,
            "version": item.workflow_version.version,
            "source_digest": item.source_digest,
        },
        "interface": interface_contract,
        "input_contract": interface_contract.get("inputs", []),
        "output_contract": interface_contract.get("outputs", []),
        "ready": item.product.is_active and snapshot_current and workflow["ready"],
        "blockers": blockers,
        "created_at": item.created_at,
    }


def _validate_workflow_version_ready(version: WorkflowVersion) -> WorkflowVersion:
    try:
        _compile_published_workflow(version)
    except Exception as error:
        raise IntegrationAPIError(
            "WORKFLOW_VERSION_NOT_RUNNABLE",
            str(error),
            category="workflow",
            details={"blockers": _workflow_version_payload(version)["blockers"]},
        ) from error
    outputs = version.interface_contract.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise IntegrationAPIError(
            "OUTPUT_CONTRACT_MISSING",
            "WorkflowVersion 没有声明语义化输出契约。",
            category="workflow",
        )
    invalid_outputs = [
        item.get("name") if isinstance(item, dict) else "<invalid>"
        for item in outputs
        if not _output_semantic_ready(item)
    ]
    if invalid_outputs:
        raise IntegrationAPIError(
            "OUTPUT_CONTRACT_INVALID",
            "WorkflowVersion 输出缺少 semantic_type。",
            category="workflow",
            details={"outputs": invalid_outputs},
        )
    return version


def _fixed_workflow(value: Any) -> WorkflowVersion:
    if not isinstance(value, dict) or value.get("source_type") != "workflow_version":
        raise IntegrationAPIError(
            "WORKFLOW_VERSION_REQUIRED",
            "workflow.source_type 必须是 workflow_version。",
            category="workflow",
        )
    try:
        version_id = int(value.get("version_id"))
    except (TypeError, ValueError):
        raise IntegrationAPIError(
            "WORKFLOW_VERSION_REQUIRED",
            "必须指定固定 workflow.version_id。",
            category="workflow",
        ) from None
    version = (
        WorkflowVersion.objects.select_related("workflow")
        .filter(
            pk=version_id,
            kind=WorkflowDocument.Kind.WORKFLOW,
            workflow__kind=WorkflowDocument.Kind.WORKFLOW,
        )
        .first()
    )
    if version is None:
        raise IntegrationAPIError(
            "WORKFLOW_VERSION_NOT_FOUND",
            "固定 WorkflowVersion 不存在。",
            category="workflow",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    expected = str(value.get("expected_source_digest") or "").strip()
    if not expected:
        raise IntegrationAPIError(
            "WORKFLOW_DIGEST_REQUIRED",
            "必须携带 workflow.expected_source_digest。",
            category="workflow",
        )
    if expected != version.compiled_digest:
        raise IntegrationAPIError(
            "WORKFLOW_VERSION_CHANGED",
            "WorkflowVersion 可执行快照摘要不一致。",
            category="workflow",
            details={"expected": expected, "actual": version.compiled_digest},
            http_status=status.HTTP_409_CONFLICT,
        )
    return _validate_workflow_version_ready(version)


def _validate_analysis_product_version_ready(
    item: AnalysisProductVersion,
) -> AnalysisProductVersion:
    if not item.product.is_active:
        raise IntegrationAPIError(
            "ANALYSIS_PRODUCT_INACTIVE",
            "分析产品已停用，不能创建新任务。",
            category="workflow",
            details=_analysis_product_reference(item),
            http_status=status.HTTP_409_CONFLICT,
        )
    if not analysis_product_version_is_current(item):
        raise IntegrationAPIError(
            "ANALYSIS_PRODUCT_SNAPSHOT_CHANGED",
            "分析产品契约与固定 WorkflowVersion 快照不一致。",
            category="workflow",
            details=_analysis_product_reference(item),
            http_status=status.HTTP_409_CONFLICT,
        )
    _validate_workflow_version_ready(item.workflow_version)
    return item


def _fixed_analysis_product(value: Any) -> AnalysisProductVersion:
    if not isinstance(value, dict):
        raise IntegrationAPIError(
            "ANALYSIS_PRODUCT_REQUIRED",
            "analysis_product 必须是 JSON object。",
            category="workflow",
        )
    analysis_code = str(value.get("analysis_code") or "").strip().lower()
    if not analysis_code or len(analysis_code) > 128:
        raise IntegrationAPIError(
            "ANALYSIS_PRODUCT_CODE_REQUIRED",
            "必须指定 analysis_product.analysis_code。",
            category="workflow",
        )
    try:
        contract_version = normalize_contract_version(value.get("contract_version"))
    except AnalysisProductError as error:
        raise IntegrationAPIError(
            error.code,
            str(error),
            category="workflow",
        ) from error
    item = (
        AnalysisProductVersion.objects.select_related(
            "product",
            "workflow_version",
            "workflow_version__workflow",
        )
        .filter(
            product__code=analysis_code,
            contract_version=contract_version,
        )
        .first()
    )
    if item is None:
        raise IntegrationAPIError(
            "ANALYSIS_PRODUCT_VERSION_NOT_FOUND",
            "分析产品或 contract_version 不存在。",
            category="workflow",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return _validate_analysis_product_version_ready(item)


def _analysis_source(
    body: dict[str, Any],
) -> tuple[WorkflowVersion, AnalysisProductVersion | None]:
    has_workflow = "workflow" in body
    has_product = "analysis_product" in body
    if has_workflow and has_product:
        raise IntegrationAPIError(
            "ANALYSIS_SOURCE_CONFLICT",
            "workflow 与 analysis_product 只能指定一个。",
            category="workflow",
        )
    if has_product:
        item = _fixed_analysis_product(body.get("analysis_product"))
        return item.workflow_version, item
    if has_workflow:
        return _fixed_workflow(body.get("workflow")), None
    raise IntegrationAPIError(
        "ANALYSIS_SOURCE_REQUIRED",
        "必须指定固定 workflow 或 analysis_product。",
        category="workflow",
    )


def _split_pair_type(value: str) -> tuple[str, str] | None:
    if not value.startswith("Pair[") or not value.endswith("]"):
        return None
    inner = value[5:-1]
    depth = 0
    for index, character in enumerate(inner):
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "," and depth == 0:
            return inner[:index].strip(), inner[index + 1 :].strip()
    return None


def _bounded_request_sha256(
    path: Path,
    *,
    expected_identity: dict[str, int],
    snapshot_budget: ResourceSnapshotBudget,
    containment_root: Path,
) -> str:
    try:
        return snapshot_budget.file_digest(
            path,
            expected_identity=expected_identity,
            containment_root=containment_root,
        )
    except ResourceSnapshotBudgetError as error:
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_LIMIT_EXCEEDED",
            str(error),
            category="input",
        ) from error
    except ResourceSnapshotChangedError as error:
        raise IntegrationAPIError(
            "ANALYSIS_RESOURCE_CHANGED",
            str(error),
            category="input",
        ) from error


def _managed_resource(
    value: Any,
    *,
    kind: str,
    input_name: str,
    semantic_type: str,
    manifests: dict[str, list[dict[str, Any]]],
    observed: list[dict[str, Any]],
    snapshot_budget: ResourceSnapshotBudget,
) -> str:
    if not isinstance(value, dict):
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_REQUIRED",
            f"输入 {input_name} 必须使用 root_alias + relative_path。",
            category="input",
        )
    if kind == "file" and value.get("identity_digest"):
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_DIGEST_INVALID",
            f"输入 {input_name} 的 File 只能使用 sha256，不能使用 identity_digest。",
            category="input",
        )
    alias = str(value.get("root_alias") or "").strip()
    relative_value = str(value.get("relative_path") or "").strip()
    if alias not in ROOT_ALIASES or not relative_value or "\x00" in relative_value:
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_INVALID",
            f"输入 {input_name} 的 root_alias 或 relative_path 无效。",
            category="input",
        )
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_PATH_INVALID",
            f"输入 {input_name} 必须使用受管目录内的相对路径。",
            category="input",
        )
    try:
        snapshot_budget.claim_item()
    except ResourceSnapshotBudgetError as error:
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_LIMIT_EXCEEDED",
            str(error),
            category="input",
        ) from error
    local_setting, execution_setting = ROOT_ALIASES[alias]
    local_root = Path(getattr(settings, local_setting)).resolve()
    execution_root = Path(getattr(settings, execution_setting)).resolve()
    local_path = local_root / relative
    try:
        normalized = local_path.absolute().relative_to(local_root.absolute())
    except ValueError as error:
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_ESCAPE",
            f"输入 {input_name} 越过受管目录。",
            category="input",
        ) from error
    try:
        stat = os.stat(local_path, follow_symlinks=False)
    except OSError:
        stat = None
    if stat is not None and stat_module.S_ISLNK(stat.st_mode):
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_ESCAPE",
            f"输入 {input_name} 越过受管目录或包含符号链接。",
            category="input",
        )
    expected_mode = stat_module.S_ISDIR if kind == "directory" else stat_module.S_ISREG
    if stat is None or not expected_mode(stat.st_mode):
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_MISSING",
            f"输入 {input_name} 的资源不存在：{normalized.as_posix()}",
            category="input",
            details={"root_alias": alias, "relative_path": normalized.as_posix()},
        )
    observed_identity = (
        {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }
        if kind == "file"
        else {
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }
    )
    if kind == "file":
        try:
            observed_identity = snapshot_budget.file_identity(
                local_path,
                containment_root=local_root,
            )
        except (OSError, ValueError) as error:
            raise IntegrationAPIError(
                "MANAGED_RESOURCE_ESCAPE",
                f"输入 {input_name} 越过受管目录或包含符号链接。",
                category="input",
            ) from error
    if kind == "file" and observed_identity["size"] == 0:
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_EMPTY",
            f"输入 {input_name} 的文件为空。",
            category="input",
        )
    declared_digest = str(
        (
            value.get("identity_digest")
            if kind == "directory"
            else value.get("sha256")
        )
        or ""
    ).strip()
    normalized_declared_digest = declared_digest.removeprefix("sha256:")
    if normalized_declared_digest and not re.fullmatch(
        r"[0-9a-fA-F]{64}", normalized_declared_digest
    ):
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_DIGEST_INVALID",
            f"输入 {input_name} 的摘要格式无效。",
            category="input",
        )
    expected_digest = (
        f"sha256:{normalized_declared_digest.lower()}"
        if normalized_declared_digest
        else ""
    )
    actual_digest = ""
    if kind == "file" and expected_digest:
        try:
            actual_digest = _bounded_request_sha256(
                local_path,
                expected_identity=observed_identity,
                snapshot_budget=snapshot_budget,
                containment_root=local_root,
            )
        except IntegrationAPIError:
            raise
        except (OSError, ValueError) as error:
            raise IntegrationAPIError(
                "MANAGED_RESOURCE_UNREADABLE",
                f"输入 {input_name} 无法读取。",
                category="input",
            ) from error
        if expected_digest and actual_digest != expected_digest:
            raise IntegrationAPIError(
                "MANAGED_RESOURCE_DIGEST_MISMATCH",
                f"输入 {input_name} 的 SHA-256 不一致。",
                category="input",
            )
    observed_directory = None
    if kind == "directory":
        try:
            observed_directory = snapshot_budget.directory_manifest(
                local_path,
                containment_root=local_root,
            )
        except ResourceSnapshotBudgetError as error:
            raise IntegrationAPIError(
                "MANAGED_RESOURCE_LIMIT_EXCEEDED",
                str(error),
                category="input",
            ) from error
        except ResourceSnapshotChangedError as error:
            raise IntegrationAPIError(
                "ANALYSIS_RESOURCE_CHANGED",
                str(error),
                category="input",
            ) from error
        except (OSError, ValueError) as error:
            raise IntegrationAPIError(
                "MANAGED_RESOURCE_UNSUPPORTED",
                f"输入 {input_name} 的目录包含不受支持的节点。",
                category="input",
            ) from error
        observed_identity = observed_directory["identity"]
    if kind == "directory" and expected_digest and observed_directory["digest"] != expected_digest:
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_DIGEST_MISMATCH",
            f"输入 {input_name} 的目录身份摘要不一致。",
            category="input",
        )
    manifest: dict[str, Any] = {
        "root_alias": alias,
        "relative_path": normalized.as_posix(),
        "kind": kind,
        "input": input_name,
        "verification": (
            "directory_identity_sha256"
            if kind == "directory"
            else "sha256"
            if expected_digest
            else "identity_v2"
        ),
        "identity": observed_identity,
    }
    if kind == "file":
        manifest.update(
            {
                "size": observed_identity["size"],
                "mtime_ns": observed_identity["mtime_ns"],
                "ctime_ns": observed_identity["ctime_ns"],
                "device": observed_identity["device"],
                "inode": observed_identity["inode"],
            }
        )
        if expected_digest:
            manifest["sha256"] = actual_digest
    else:
        manifest.update(
            {
                "digest": observed_directory["digest"],
                "entry_count": observed_directory["entry_count"],
            }
        )
        if expected_digest:
            manifest["declared_identity_digest"] = expected_digest
        if value.get("sha256"):
            manifest["warning"] = "legacy_directory_sha256_ignored"
    manifests[alias].append(manifest)
    observed.append(
        {
            "input": input_name,
            "semantic_type": semantic_type,
            "kind": kind,
            "path": local_path,
            "containment_root": local_root,
            "identity": observed_identity,
        }
    )
    return str(execution_root / normalized)


def _coerce_input(
    value: Any,
    *,
    wdl_type: str,
    input_name: str,
    semantic_type: str,
    manifests: dict[str, list[dict[str, Any]]],
    observed: list[dict[str, Any]],
    snapshot_budget: ResourceSnapshotBudget,
) -> Any:
    optional = wdl_type.endswith("?")
    normalized_type = wdl_type.removesuffix("?").strip()
    if value is None and optional:
        return None
    if normalized_type == "File":
        return _managed_resource(
            value,
            kind="file",
            input_name=input_name,
            semantic_type=semantic_type,
            manifests=manifests,
            observed=observed,
            snapshot_budget=snapshot_budget,
        )
    if normalized_type == "Directory":
        return _managed_resource(
            value,
            kind="directory",
            input_name=input_name,
            semantic_type=semantic_type,
            manifests=manifests,
            observed=observed,
            snapshot_budget=snapshot_budget,
        )
    if normalized_type.startswith("Array[") and normalized_type.endswith("]"):
        if not isinstance(value, list):
            raise IntegrationAPIError(
                "INPUT_TYPE_INVALID",
                f"输入 {input_name} 必须是数组。",
                category="input",
            )
        subtype = normalized_type[6:-1].strip()
        return [
            _coerce_input(
                item,
                wdl_type=subtype,
                input_name=input_name,
                semantic_type=semantic_type,
                manifests=manifests,
                observed=observed,
                snapshot_budget=snapshot_budget,
            )
            for item in value
        ]
    pair = _split_pair_type(normalized_type)
    if pair:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise IntegrationAPIError(
                "INPUT_TYPE_INVALID",
                f"输入 {input_name} 必须是两个元素的 Pair。",
                category="input",
            )
        return {
            side: _coerce_input(
                value[index],
                wdl_type=pair[index],
                input_name=input_name,
                semantic_type=semantic_type,
                manifests=manifests,
                observed=observed,
                snapshot_budget=snapshot_budget,
            )
            for index, side in enumerate(("left", "right"))
        }
    checks = {
        "String": lambda item: isinstance(item, str),
        "Int": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "Float": lambda item: isinstance(item, (int, float))
        and not isinstance(item, bool),
        "Boolean": lambda item: isinstance(item, bool),
    }
    if normalized_type not in checks:
        raise IntegrationAPIError(
            "INPUT_TYPE_UNSUPPORTED",
            f"输入 {input_name} 的 WDL 类型 {wdl_type} 暂不支持。",
            category="input",
        )
    if not checks[normalized_type](value):
        raise IntegrationAPIError(
            "INPUT_TYPE_INVALID",
            f"输入 {input_name} 不符合 {wdl_type} 类型。",
            category="input",
        )
    return float(value) if normalized_type == "Float" else value


def _snapshot_checkpoint(snapshot_budget: ResourceSnapshotBudget) -> None:
    try:
        snapshot_budget.checkpoint()
    except ResourceSnapshotBudgetError as error:
        raise IntegrationAPIError(
            "MANAGED_RESOURCE_LIMIT_EXCEEDED",
            str(error),
            category="input",
        ) from error


def _bounded_text_line(
    handle,
    *,
    path: Path,
    snapshot_budget: ResourceSnapshotBudget,
) -> str:
    _snapshot_checkpoint(snapshot_budget)
    limit = settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS
    line = handle.readline(limit + 1)
    _snapshot_checkpoint(snapshot_budget)
    if len(line) > limit:
        raise IntegrationAPIError(
            "INPUT_RECORD_TOO_LARGE",
            f"输入文件单行超过安全上限：{path.name}",
            category="input",
            details={"max_chars": limit},
        )
    return line.rstrip("\r\n")


def _validate_fastq(
    path: Path,
    *,
    expected_mate: int,
    expected_identity: dict[str, int],
    containment_root: Path,
    snapshot_budget: ResourceSnapshotBudget,
) -> str:
    if not path.name.lower().endswith((".fastq.gz", ".fq.gz")):
        raise IntegrationAPIError(
            "FASTQ_EXTENSION_INVALID",
            f"FASTQ 文件扩展名无效：{path.name}",
            category="input",
        )
    try:
        _snapshot_checkpoint(snapshot_budget)
        with _open_regular_readonly(
            path,
            containment_root=containment_root,
        ) as raw_handle:
            before = os.fstat(raw_handle.fileno())
            if _file_identity(before) != expected_identity:
                raise IntegrationAPIError(
                    "ANALYSIS_RESOURCE_CHANGED",
                    f"FASTQ 在内容校验前发生变化：{path.name}",
                    category="input",
                )
            lines = _read_gzip_text_lines(
                raw_handle,
                line_count=4,
                max_chars=settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS,
                encoding="utf-8",
                checkpoint=lambda: _snapshot_checkpoint(snapshot_budget),
            )
            header, sequence, plus, quality = lines
            after = os.fstat(raw_handle.fileno())
        with _open_regular_readonly(
            path,
            containment_root=containment_root,
        ) as current_handle:
            current = os.fstat(current_handle.fileno())
        if (
            _file_identity(after) != expected_identity
            or _file_identity(current) != expected_identity
        ):
            raise IntegrationAPIError(
                "ANALYSIS_RESOURCE_CHANGED",
                f"FASTQ 在内容校验期间发生变化：{path.name}",
                category="input",
            )
    except IntegrationAPIError:
        raise
    except GzipProbeLineLimitError as error:
        raise IntegrationAPIError(
            "INPUT_RECORD_TOO_LARGE",
            f"输入文件单行超过安全上限：{path.name}",
            category="input",
            details={"max_chars": settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS},
        ) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise IntegrationAPIError(
            "FASTQ_GZIP_INVALID",
            f"FASTQ gzip 内容无效：{path.name}",
            category="input",
        ) from error
    if (
        not header.startswith("@")
        or not header[1:].split()
        or not sequence
        or not plus.startswith("+")
    ):
        raise IntegrationAPIError(
            "FASTQ_RECORD_INVALID",
            f"FASTQ 首条记录结构无效：{path.name}",
            category="input",
        )
    if len(sequence) != len(quality):
        raise IntegrationAPIError(
            "FASTQ_RECORD_INVALID",
            f"FASTQ 首条序列与质量长度不一致：{path.name}",
            category="input",
        )
    parts = header[1:].split()
    slash_mate = re.search(r"/([12])$", parts[0])
    if slash_mate and int(slash_mate.group(1)) != expected_mate:
        raise IntegrationAPIError(
            "FASTQ_MATE_INVALID",
            f"FASTQ 声明的 mate 与输入语义不一致：{path.name}",
            category="input",
        )
    read_id = re.sub(r"/[12]$", "", parts[0])
    if len(parts) > 1 and re.match(r"^[12]:", parts[1]):
        mate = int(parts[1][0])
        if mate != expected_mate:
            raise IntegrationAPIError(
                "FASTQ_MATE_INVALID",
                f"FASTQ 声明的 mate 与输入语义不一致：{path.name}",
                category="input",
            )
    return read_id


def _validate_fasta(
    path: Path,
    *,
    expected_identity: dict[str, int],
    containment_root: Path,
    snapshot_budget: ResourceSnapshotBudget,
) -> None:
    try:
        _snapshot_checkpoint(snapshot_budget)
        with _open_regular_readonly(
            path,
            containment_root=containment_root,
        ) as raw_handle:
            before = os.fstat(raw_handle.fileno())
            if _file_identity(before) != expected_identity:
                raise IntegrationAPIError(
                    "ANALYSIS_RESOURCE_CHANGED",
                    f"FASTA 在内容校验前发生变化：{path.name}",
                    category="input",
                )
            if path.name.lower().endswith(".gz"):
                first_line = _read_gzip_text_lines(
                    raw_handle,
                    line_count=1,
                    max_chars=settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS,
                    encoding="utf-8",
                    checkpoint=lambda: _snapshot_checkpoint(snapshot_budget),
                )[0]
            else:
                first = raw_handle.readline(
                    settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS + 1
                )
                _snapshot_checkpoint(snapshot_budget)
                if len(first) > settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS:
                    raise IntegrationAPIError(
                        "INPUT_RECORD_TOO_LARGE",
                        f"输入文件单行超过安全上限：{path.name}",
                        category="input",
                    )
                first_line = first.decode("utf-8", errors="strict").rstrip("\r\n")
            after = os.fstat(raw_handle.fileno())
        with _open_regular_readonly(
            path,
            containment_root=containment_root,
        ) as current_handle:
            current = os.fstat(current_handle.fileno())
        if (
            _file_identity(after) != expected_identity
            or _file_identity(current) != expected_identity
        ):
            raise IntegrationAPIError(
                "ANALYSIS_RESOURCE_CHANGED",
                f"FASTA 在内容校验期间发生变化：{path.name}",
                category="input",
            )
    except IntegrationAPIError:
        raise
    except GzipProbeLineLimitError as error:
        raise IntegrationAPIError(
            "INPUT_RECORD_TOO_LARGE",
            f"输入文件单行超过安全上限：{path.name}",
            category="input",
            details={"max_chars": settings.ANALYSIS_INPUT_TEXT_LINE_MAX_CHARS},
        ) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise IntegrationAPIError(
            "FASTA_CONTENT_INVALID",
            f"FASTA 内容无法读取：{path.name}",
            category="input",
        ) from error
    if not first_line.startswith(">"):
        raise IntegrationAPIError(
            "FASTA_CONTENT_INVALID",
            f"FASTA 第一行缺少 > header：{path.name}",
            category="input",
        )


def _content_checks(
    observed: list[dict[str, Any]],
    *,
    snapshot_budget: ResourceSnapshotBudget,
) -> list[dict[str, Any]]:
    reads: dict[int, list[str]] = {1: [], 2: []}
    checked = []
    for item in observed:
        _snapshot_checkpoint(snapshot_budget)
        semantic = item["semantic_type"]
        path = item["path"]
        if semantic == "bio.fastq.gz.r1":
            reads[1].append(
                _validate_fastq(
                    path,
                    expected_mate=1,
                    expected_identity=item["identity"],
                    containment_root=item["containment_root"],
                    snapshot_budget=snapshot_budget,
                )
            )
            checked.append({"input": item["input"], "check": "fastq_r1", "ready": True})
        elif semantic == "bio.fastq.gz.r2":
            reads[2].append(
                _validate_fastq(
                    path,
                    expected_mate=2,
                    expected_identity=item["identity"],
                    containment_root=item["containment_root"],
                    snapshot_budget=snapshot_budget,
                )
            )
            checked.append({"input": item["input"], "check": "fastq_r2", "ready": True})
        elif item["kind"] == "file" and "fasta" in semantic.casefold():
            _validate_fasta(
                path,
                expected_identity=item["identity"],
                containment_root=item["containment_root"],
                snapshot_budget=snapshot_budget,
            )
            checked.append({"input": item["input"], "check": "fasta", "ready": True})
    if reads[1] or reads[2]:
        if len(reads[1]) != len(reads[2]) or reads[1] != reads[2]:
            raise IntegrationAPIError(
                "FASTQ_PAIR_MISMATCH",
                "R1/R2 FASTQ 首条 read ID 不配对。",
                category="input",
                details={"r1": reads[1], "r2": reads[2]},
            )
        checked.append({"check": "fastq_pair", "ready": True})
    return checked


def _prepare_contract_inputs(
    ports: list[dict[str, Any]],
    raw_inputs: Any,
    *,
    workflow_name: str,
    input_keys: dict[str, str] | None = None,
    snapshot_budget: ResourceSnapshotBudget | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(raw_inputs, dict):
        raise IntegrationAPIError("INPUTS_INVALID", "inputs 必须是 JSON object。", category="input")
    by_name = {str(port.get("name")): port for port in ports if isinstance(port, dict)}
    unknown = sorted(set(raw_inputs) - set(by_name))
    if unknown:
        raise IntegrationAPIError(
            "INPUT_UNKNOWN",
            "请求包含 Workflow 未声明的输入。",
            category="input",
            details={"inputs": unknown},
        )
    manifests: dict[str, list[dict[str, Any]]] = {"rawdata": [], "database": []}
    snapshot_budget = snapshot_budget or ResourceSnapshotBudget()
    observed: list[dict[str, Any]] = []
    values: dict[str, Any] = {}
    for name, port in by_name.items():
        if name in raw_inputs:
            raw_value = raw_inputs[name]
        elif "default" in port:
            raw_value = port["default"]
        elif not port.get("required", True) or str(port.get("wdl_type", "")).endswith("?"):
            continue
        else:
            raise IntegrationAPIError(
                "INPUT_REQUIRED",
                f"缺少必填输入 {name}。",
                category="input",
                details={"input": name},
            )
        value = _coerce_input(
            raw_value,
            wdl_type=str(port.get("wdl_type") or ""),
            input_name=name,
            semantic_type=str(port.get("semantic_type") or "core.value.unknown"),
            manifests=manifests,
            observed=observed,
            snapshot_budget=snapshot_budget,
        )
        try:
            _validate_constraints(port, value)
        except ValueError as error:
            raise IntegrationAPIError(
                "INPUT_CONSTRAINT_INVALID",
                str(error),
                category="input",
            ) from error
        key = input_keys[name] if input_keys else name
        values[f"{workflow_name}.{key}"] = value
    content_checks = _content_checks(
        observed,
        snapshot_budget=snapshot_budget,
    )
    resource_manifests = {
        "input_resource_manifest": (
            {"schema_version": 1, "files": manifests["rawdata"]}
            if manifests["rawdata"]
            else None
        ),
        "database_resource_manifest": (
            {"schema_version": 1, "resources": manifests["database"]}
            if manifests["database"]
            else None
        ),
    }
    return values, resource_manifests, content_checks


def _declared_resources(
    version: WorkflowVersion,
    *,
    snapshot_budget: ResourceSnapshotBudget | None = None,
) -> dict[str, Any]:
    resources = version.interface_contract.get("resources") or []
    if not resources:
        return {"database_resource_manifest": None, "checks": []}
    manifests = {"rawdata": [], "database": []}
    snapshot_budget = snapshot_budget or ResourceSnapshotBudget()
    observed: list[dict[str, Any]] = []
    for index, resource in enumerate(resources, 1):
        if not isinstance(resource, dict):
            raise IntegrationAPIError(
                "WORKFLOW_RESOURCE_CONTRACT_INVALID",
                "WorkflowVersion resources 契约无效。",
                category="workflow",
            )
        kind = str(resource.get("kind") or "file")
        if kind not in {"file", "directory"}:
            raise IntegrationAPIError(
                "WORKFLOW_RESOURCE_CONTRACT_INVALID",
                "WorkflowVersion resources.kind 只支持 file 或 directory。",
                category="workflow",
                details={"kind": kind},
            )
        _managed_resource(
            resource,
            kind=kind,
            input_name=str(resource.get("name") or f"resource_{index}"),
            semantic_type=str(resource.get("semantic_type") or "core.resource"),
            manifests=manifests,
            observed=observed,
            snapshot_budget=snapshot_budget,
        )
    return {
        "database_resource_manifest": (
            {"schema_version": 1, "resources": manifests["database"]}
            if manifests["database"]
            else None
        ),
        "checks": [
            {"check": "workflow_declared_resource", "input": item["input"], "ready": True}
            for item in observed
        ],
    }


def _catalog_entry(
    entries: Any,
    entry_id: str,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(entries, list):
        entries = []
    entry = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and str(item.get("id") or "") == entry_id
        ),
        None,
    )
    if entry is None:
        raise IntegrationAPIError(
            "DATABASE_CATALOG_ENTRY_NOT_FOUND",
            f"数据库 Catalog 中不存在所选{label}。",
            category="resource",
            details={"kind": label, "id": entry_id},
        )
    return entry


def _catalog_resources(
    version: WorkflowVersion,
    body: dict[str, Any],
    *,
    snapshot_budget: ResourceSnapshotBudget | None = None,
) -> dict[str, Any]:
    contract = version.interface_contract.get("database") or {}
    if not isinstance(contract, dict):
        raise IntegrationAPIError(
            "WORKFLOW_DATABASE_CONTRACT_INVALID",
            "WorkflowVersion 数据库契约无效。",
            category="workflow",
        )
    database_inputs = {
        str(item.get("semantic_type") or "")
        for item in _workflow_interface(version)
        if isinstance(item, dict)
    }
    reference_required = bool(contract.get("required")) or bool(
        database_inputs & {"bio.annotation.database_dir", "bio.reference.database_dir"}
    )
    panel_required = bool(contract.get("panel_required"))
    selection = body.get("database")
    if selection is None and not reference_required and not panel_required:
        return {"manifests": {}, "checks": []}
    if not isinstance(selection, dict):
        raise IntegrationAPIError(
            "DATABASE_SELECTION_REQUIRED",
            "该 WorkflowVersion 必须指定 database.reference_id。",
            category="resource",
        )
    reference_id = str(
        selection.get("reference_id") or contract.get("reference_id") or ""
    ).strip()
    panel_id = str(selection.get("panel_id") or contract.get("panel_id") or "").strip()
    if reference_required and not reference_id:
        raise IntegrationAPIError(
            "DATABASE_REFERENCE_REQUIRED",
            "该 WorkflowVersion 必须指定 database.reference_id。",
            category="resource",
        )
    if panel_required and not panel_id:
        raise IntegrationAPIError(
            "DATABASE_PANEL_REQUIRED",
            "该 WorkflowVersion 必须指定 database.panel_id。",
            category="resource",
        )
    allowed_references = contract.get("allowed_reference_ids") or []
    allowed_panels = contract.get("allowed_panel_ids") or []
    if allowed_references and reference_id not in allowed_references:
        raise IntegrationAPIError(
            "DATABASE_REFERENCE_NOT_ALLOWED",
            "所选参考版本不在 WorkflowVersion 允许范围内。",
            category="resource",
            details={"reference_id": reference_id},
        )
    if allowed_panels and panel_id not in allowed_panels:
        raise IntegrationAPIError(
            "DATABASE_PANEL_NOT_ALLOWED",
            "所选 Panel 不在 WorkflowVersion 允许范围内。",
            category="resource",
            details={"panel_id": panel_id},
        )
    try:
        catalog = load_database_catalog()
    except AnalysisInputError as error:
        raise IntegrationAPIError(
            error.code,
            str(error),
            category="resource",
            details=error.details,
        ) from error
    reference = (
        _catalog_entry(catalog.get("references"), reference_id, label="参考版本")
        if reference_id
        else None
    )
    panel = (
        _catalog_entry(catalog.get("panels"), panel_id, label="Panel")
        if panel_id
        else None
    )
    if panel is not None and reference_id and str(panel.get("reference") or "") not in {
        "",
        reference_id,
    }:
        raise IntegrationAPIError(
            "DATABASE_PANEL_REFERENCE_MISMATCH",
            "所选 Panel 与参考版本不匹配。",
            category="resource",
            details={"reference_id": reference_id, "panel_id": panel_id},
        )
    snapshot_budget = snapshot_budget or ResourceSnapshotBudget()
    try:
        missing = [
            item
            for entry in (reference, panel)
            if entry is not None
            for item in _requirements(entry, snapshot_budget=snapshot_budget)
            if not item["present"]
        ]
    except AnalysisInputError as error:
        raise IntegrationAPIError(
            error.code,
            str(error),
            category="resource",
            details=error.details,
        ) from error
    if missing:
        raise IntegrationAPIError(
            "ANALYSIS_DATABASE_INCOMPLETE",
            f"数据库资源尚缺 {len(missing)} 项。",
            category="resource",
            details={"missing": missing},
        )
    manifests: dict[str, Any] = {
        "database_catalog_digest": _canonical_digest(catalog),
        "database_selection": {
            "reference_id": reference_id or None,
            "panel_id": panel_id or None,
        },
    }
    checks = []
    try:
        if reference is not None:
            manifest = _catalog_resource_manifest(
                reference,
                snapshot_budget=snapshot_budget,
            )
            manifests["reference_resource_manifest"] = manifest
            manifests["reference_digest"] = _canonical_digest(manifest)
            checks.append(
                {"check": "database_reference", "ready": True, "id": reference_id}
            )
        if panel is not None:
            manifest = _catalog_resource_manifest(
                panel,
                snapshot_budget=snapshot_budget,
            )
            manifests["panel_resource_manifest"] = manifest
            manifests["panel_digest"] = _canonical_digest(manifest)
            checks.append({"check": "database_panel", "ready": True, "id": panel_id})
    except AnalysisInputError as error:
        raise IntegrationAPIError(
            error.code,
            str(error),
            category="resource",
            details=error.details,
        ) from error
    return {"manifests": manifests, "checks": checks}


def _workflow_output_contract(version: WorkflowVersion) -> list[dict[str, Any]]:
    workflow_name = str(version.workflow_graph.get("id") or version.workflow.slug)
    return [
        {
            "key": f"{workflow_name}.{item['name']}",
            "name": item["name"],
            "label": item.get("label") or item["name"],
            "semantic_type": item["semantic_type"],
            "wdl_type": item.get("wdl_type") or "String",
            "required": bool(item.get("required", False)),
        }
        for item in version.interface_contract.get("outputs", [])
    ]


def _preflight_workflow(body: dict[str, Any]) -> dict[str, Any]:
    version, product_version = _analysis_source(body)
    workflow_name = str(version.workflow_graph.get("id") or version.workflow.slug)
    snapshot_budget = ResourceSnapshotBudget()
    input_values, manifests, content_checks = _prepare_contract_inputs(
        _workflow_interface(version),
        body.get("inputs") or {},
        workflow_name=workflow_name,
        snapshot_budget=snapshot_budget,
    )
    declared = _declared_resources(version, snapshot_budget=snapshot_budget)
    catalog = _catalog_resources(
        version,
        body,
        snapshot_budget=snapshot_budget,
    )
    if declared["database_resource_manifest"]:
        existing = manifests.get("database_resource_manifest") or {
            "schema_version": 1,
            "resources": [],
        }
        existing["resources"].extend(
            declared["database_resource_manifest"]["resources"]
        )
        manifests["database_resource_manifest"] = existing
    manifests.update(catalog["manifests"])
    available = _available_memory_bytes()
    minimum_gb = max(0.0, float(settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB))
    resource_ready = available is None or available >= minimum_gb * 1024**3
    return {
        "workflow_version": version,
        "analysis_product_version": product_version,
        "analysis_product": (
            _analysis_product_version_payload(product_version)
            if product_version is not None
            else None
        ),
        "workflow": _workflow_version_payload(version),
        "input_values": input_values,
        "manifests": manifests,
        "output_contract": _workflow_output_contract(version),
        "checks": [
            {"check": "workflow_snapshot", "ready": True},
            *content_checks,
            *declared["checks"],
            *catalog["checks"],
            {
                "check": "execution_memory",
                "ready": resource_ready,
                "blocking": False,
                "available_gb": round(available / 1024**3, 2) if available is not None else None,
                "minimum_gb": minimum_gb,
            },
            {"check": "output_contract", "ready": True},
        ],
    }


def _tool_output_contract(item: ToolVersion) -> list[dict[str, Any]]:
    return [
        {
            "key": f"tool_test.{_safe_identifier('output_', index, str(port['name']))}",
            "name": port["name"],
            "label": port.get("label") or port["name"],
            "semantic_type": port.get("semantic_type") or "core.output.unknown",
            "wdl_type": port.get("wdl_type") or "String",
            "required": not bool(port.get("optional", False)),
        }
        for index, port in enumerate(item.tool_spec.get("outputs", []), 1)
    ]


def _preflight_tool(body: dict[str, Any]) -> dict[str, Any]:
    tool = body.get("tool")
    if not isinstance(tool, dict):
        raise IntegrationAPIError("TOOL_VERSION_REQUIRED", "必须指定固定 ToolVersion。")
    tool_id = str(tool.get("tool_id") or "").strip()
    version_value = str(tool.get("version") or "").strip()
    item = ToolVersion.objects.filter(tool_id=tool_id, version=version_value).first()
    if item is None:
        raise IntegrationAPIError(
            "TOOL_VERSION_NOT_FOUND",
            "ToolVersion 不存在。",
            category="workflow",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    expected = str(tool.get("expected_digest") or "").strip()
    if expected != item.digest:
        raise IntegrationAPIError(
            "TOOL_VERSION_CHANGED",
            "ToolVersion 摘要不一致。",
            category="workflow",
            details={"expected": expected, "actual": item.digest},
            http_status=status.HTTP_409_CONFLICT,
        )
    try:
        bundle, source_digest, input_nodes, _ = _tool_test_bundle(item)
    except ValueError as error:
        raise IntegrationAPIError(
            "TOOL_VERSION_NOT_RUNNABLE",
            str(error),
            category="workflow",
        ) from error
    snapshot_budget = ResourceSnapshotBudget()
    input_values, manifests, content_checks = _prepare_contract_inputs(
        item.tool_spec.get("inputs", []),
        body.get("inputs") or {},
        workflow_name="tool_test",
        input_keys=input_nodes,
        snapshot_budget=snapshot_budget,
    )
    return {
        "tool_version": item,
        "bundle": bundle,
        "source_digest": source_digest,
        "input_values": input_values,
        "manifests": manifests,
        "output_contract": _tool_output_contract(item),
        "checks": [
            {"check": "tool_snapshot", "ready": True},
            *content_checks,
            {"check": "output_contract", "ready": True},
        ],
    }


def _integration_error(run: AnalysisRun) -> dict[str, Any] | None:
    if not run.error_code and not run.error:
        return None
    return {
        "code": run.error_code or "ANALYSIS_FAILED",
        "category": run.error_category or "application",
        "message": _public_text(run.error),
        "retryable": run.error_retryable,
        "details": _public_value(run.error_details),
    }


def _public_text(value: Any) -> str:
    result = str(value or "")
    roots = {
        str(getattr(settings, name, "") or "")
        for name in (
            "ANALYSIS_RAWDATA_ROOT",
            "ANALYSIS_RAWDATA_EXECUTION_ROOT",
            "ANALYSIS_DATABASE_ROOT",
            "ANALYSIS_DATABASE_EXECUTION_ROOT",
            "ANALYSIS_RUN_ROOT",
            "ANALYSIS_RUN_EXECUTION_ROOT",
        )
    }
    for root in sorted((item for item in roots if item), key=len, reverse=True):
        result = result.replace(root.rstrip("/"), "<managed-root>")
    return result


def _public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _public_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, str):
        return _public_text(value)
    return value


def integration_run_payload(
    run: AnalysisRun,
    *,
    include_outputs: bool = True,
    include_task_timing: bool = True,
    include_error_details: bool = True,
    attempt: int | None = None,
) -> dict[str, Any]:
    if run.workflow_version_id:
        source = {
            "source_type": "workflow_version",
            "version_id": run.workflow_version_id,
            "slug": run.workflow_version.workflow.slug,
            "version": run.workflow_version.version,
            "source_digest": run.source_digest,
        }
    else:
        source = {
            "source_type": "tool_version",
            "tool_id": run.tool_version.tool_id,
            "version": run.tool_version.version,
            "tool_digest": run.tool_version.digest,
            "source_digest": run.source_digest,
        }
    error = _integration_error(run) if include_error_details else None
    if not include_error_details and run.error_code:
        error = {
            "code": run.error_code,
            "message": "运行失败；请读取任务详情。",
            "category": run.error_category,
            "retryable": run.error_retryable,
            "details": {},
        }
    if attempt is None:
        attempt = int(run.request_payload.get("attempt") or 1)
    return {
        "id": str(run.id),
        "external_ref": {
            "client_id": run.service_account.client_id,
            "external_run_id": run.external_run_id,
            "external_analysis_id": run.external_analysis_id,
        },
        "analysis_product": (
            _analysis_product_reference(run.analysis_product_version)
            if run.analysis_product_version_id
            else None
        ),
        "run_kind": run.run_kind,
        "workflow": source,
        "status": run.status,
        "status_version": run.status_version,
        "execution_status": run.status,
        "output_status": run.output_status,
        "progress": run.progress,
        "current_step": run.current_step,
        "attempt": attempt,
        "retry_of": str(run.retry_of_id) if run.retry_of_id else None,
        "actor": run.actor,
        "error": error,
        "outputs": public_output_manifest(run) if include_outputs else [],
        "timing": _run_timing_payload(
            run,
            include_task_timing=include_task_timing,
        ),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.updated_at,
    }


def _find_idempotent_run(
    account: ServiceAccount,
    *,
    external_run_id: str,
    idempotency_key: str,
) -> AnalysisRun | None:
    matches = list(
        AnalysisRun.objects.select_related(
            "service_account",
            "analysis_product_version",
            "analysis_product_version__product",
            "workflow_version",
            "workflow_version__workflow",
            "tool_version",
            "retry_of",
        ).filter(service_account=account).filter(
            Q(external_run_id=external_run_id) | Q(idempotency_key=idempotency_key)
        )[:2]
    )
    if len(matches) > 1:
        raise IntegrationAPIError(
            "IDEMPOTENCY_CONFLICT",
            "external_run_id 与 Idempotency-Key 已指向不同任务。",
            http_status=status.HTTP_409_CONFLICT,
        )
    return matches[0] if matches else None


def _idempotent_response(request, run: AnalysisRun, digest: str) -> Response:
    if run.request_digest != digest:
        return _error_response(
            request,
            IntegrationAPIError(
                "IDEMPOTENCY_CONFLICT",
                "相同外部任务标识已用于不同请求。",
                details={"run_id": str(run.id)},
                http_status=status.HTTP_409_CONFLICT,
            ),
        )
    response = Response(integration_run_payload(run), status=status.HTTP_200_OK)
    response["Idempotency-Replayed"] = "true"
    return response


@require_service_scopes("workflow:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_products(request):
    versions = (
        AnalysisProductVersion.objects.select_related(
            "product",
            "workflow_version",
            "workflow_version__workflow",
        )
        .filter(product__is_active=True)
        .order_by("product__code", "contract_version")
    )
    return Response(
        {"results": [_analysis_product_version_payload(item) for item in versions[:200]]}
    )


@require_service_scopes("workflow:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_product_version_detail(
    request,
    analysis_code: str,
    contract_version: str,
):
    item = get_object_or_404(
        AnalysisProductVersion.objects.select_related(
            "product",
            "workflow_version",
            "workflow_version__workflow",
        ),
        product__code=analysis_code,
        contract_version=contract_version,
    )
    return Response(_analysis_product_version_payload(item))


@require_service_scopes("workflow:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_workflow_versions(request):
    versions = WorkflowVersion.objects.select_related("workflow").filter(
        kind=WorkflowDocument.Kind.WORKFLOW,
        workflow__kind=WorkflowDocument.Kind.WORKFLOW,
    )
    return Response({"results": [_workflow_version_payload(item) for item in versions[:200]]})


@require_service_scopes("workflow:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_workflow_version_detail(request, version_id: int):
    item = get_object_or_404(
        WorkflowVersion.objects.select_related("workflow"),
        pk=version_id,
        kind=WorkflowDocument.Kind.WORKFLOW,
    )
    return Response(_workflow_version_payload(item))


@require_service_scopes("analysis:submit")
@api_view(["POST"])
@permission_classes([IntegrationScopePermission])
def integration_preflight(request):
    try:
        metadata = _validate_metadata(request.data.get("metadata"))
        result = _preflight_workflow(dict(request.data))
    except IntegrationAPIError as error:
        return _error_response(request, error)
    return Response(
        {
            "ready": all(bool(item.get("ready")) for item in result["checks"]),
            "submission_allowed": True,
            "waiting_for": [
                item["check"]
                for item in result["checks"]
                if not item.get("ready") and not item.get("blocking", True)
            ],
            "workflow": result["workflow"],
            "analysis_product": result["analysis_product"],
            "checks": result["checks"],
            "resource_manifest": result["manifests"],
            "output_contract": result["output_contract"],
            "metadata": metadata,
        }
    )


@require_service_scopes_by_method(
    GET=("analysis:read",),
    POST=("analysis:submit",),
)
@api_view(["GET", "POST"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_runs(request):
    if request.method == "GET":
        queryset = (
            _visible_runs(request)
            .filter(run_kind=AnalysisRun.Kind.WORKFLOW)
            .annotate(
                list_attempt=Cast(
                    "request_payload__attempt",
                    output_field=IntegerField(),
                )
            )
            .defer(
                "outputs",
                "output_manifest",
                "request_payload",
                "input_values",
                "source_bundle",
                "error",
                "error_details",
                "work_directory",
                "workflow_version__workflow_graph",
                "workflow_version__editor_document",
                "workflow_version__tool_specs",
                "workflow_version__compiled_bundle",
                "workflow_version__interface_contract",
                "workflow_version__subworkflow_references",
                "tool_version__tool_spec",
            )
        )
        if request.query_params.get("active") == "1":
            queryset = queryset.exclude(status__in=TERMINAL_STATUSES)
        return Response(
            {
                "view": "summary",
                "results": [
                    integration_run_payload(
                        run,
                        include_outputs=False,
                        include_task_timing=False,
                        include_error_details=False,
                        attempt=int(run.list_attempt or 1),
                    )
                    for run in queryset[:200]
                ]
            }
        )
    try:
        body = dict(request.data)
        external = _external_ref(body.get("external_ref"))
        account = _request_service_account(request, external)
        external["client_id"] = account.client_id
        idempotency_key = _idempotency_key(request)
        _validate_metadata(body.get("metadata"))
        if not isinstance(body.get("subject"), dict):
            raise IntegrationAPIError("SUBJECT_INVALID", "subject 必须是 JSON object。")
        _validate_metadata(body["subject"], field="subject")
        sample_id = str(body["subject"].get("sample_id") or "").strip()
        if not EXTERNAL_ID_PATTERN.fullmatch(sample_id):
            raise IntegrationAPIError("SAMPLE_ID_INVALID", "subject.sample_id 格式无效。")
        canonical_body = {**body, "external_ref": external}
        digest = _canonical_digest({"kind": "workflow", "request": canonical_body})
        existing = _find_idempotent_run(
            account,
            external_run_id=external["external_run_id"],
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return _idempotent_response(request, existing, digest)
        preflight = _preflight_workflow(body)
        version = preflight["workflow_version"]
        product_version = preflight["analysis_product_version"]
        source_bundle, source_digest = _compile_published_workflow(version)
        request_payload = {
            "kind": "integration_workflow",
            "attempt": 1,
            "external_ref": external,
            "subject": body["subject"],
            "metadata": body.get("metadata") or {},
            "managed_inputs": body.get("inputs") or {},
            "workflow_semantic_digest": version.semantic_digest,
            "compiled_source_digest": source_digest,
            "integration_output_contract": preflight["output_contract"],
            **preflight["manifests"],
        }
        if product_version is not None:
            request_payload["analysis_product"] = {
                **_analysis_product_reference(product_version),
                "workflow_version_id": version.pk,
                "source_digest": source_digest,
            }
        try:
            with transaction.atomic():
                run = AnalysisRun.objects.create(
                    run_kind=AnalysisRun.Kind.WORKFLOW,
                    workflow_version=version,
                    analysis_product_version=product_version,
                    service_account=account,
                    external_run_id=external["external_run_id"],
                    external_analysis_id=external["external_analysis_id"],
                    idempotency_key=idempotency_key,
                    request_digest=digest,
                    workflow_name=str(version.workflow_graph.get("id") or version.workflow.slug),
                    sample_id=sample_id,
                    sample_name=sample_id,
                    actor=_actor(request),
                    source_bundle=source_bundle,
                    source_digest=source_digest,
                    request_payload=request_payload,
                    input_values=preflight["input_values"],
                )
                AnalysisRunEvent.objects.create(
                    run=run,
                    message="外部分析任务已通过预检并进入队列。",
                    details={"status_version": run.status_version},
                )
        except IntegrityError:
            run = _find_idempotent_run(
                account,
                external_run_id=external["external_run_id"],
                idempotency_key=idempotency_key,
            )
            if run is None:
                raise
            return _idempotent_response(request, run, digest)
    except IntegrationAPIError as error:
        return _error_response(request, error)
    return Response(integration_run_payload(run), status=status.HTTP_201_CREATED)


@require_service_scopes("analysis:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_run_detail(request, run_id):
    run = get_object_or_404(_visible_runs(request), pk=run_id)
    return Response(integration_run_payload(run))


@require_service_scopes("analysis:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_run_by_external_ref(request):
    external_run_id = str(request.query_params.get("external_run_id") or "").strip()
    if not EXTERNAL_ID_PATTERN.fullmatch(external_run_id):
        return _error_response(
            request,
            IntegrationAPIError("EXTERNAL_RUN_ID_INVALID", "external_run_id 格式无效。"),
        )
    queryset = _visible_runs(request).filter(external_run_id=external_run_id)
    account = _service_account(request)
    if account is None:
        client_id = str(request.query_params.get("client_id") or "").strip()
        if not client_id:
            return _error_response(
                request,
                IntegrationAPIError("EXTERNAL_CLIENT_REQUIRED", "管理员查询必须指定 client_id。"),
            )
        queryset = queryset.filter(service_account__client_id=client_id)
    run = get_object_or_404(queryset)
    return Response(integration_run_payload(run))


@require_service_scopes("analysis:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_run_events(request, run_id):
    run = get_object_or_404(_visible_runs(request), pk=run_id)
    try:
        after_id = max(0, int(request.query_params.get("after_id") or 0))
    except ValueError:
        return _error_response(
            request,
            IntegrationAPIError("EVENT_CURSOR_INVALID", "after_id 必须是非负整数。"),
        )
    events = list(run.events.filter(id__gt=after_id).order_by("id")[:500])
    return Response(
        {
            "run_id": str(run.id),
            "status": run.status,
            "status_version": run.status_version,
            "results": [
                {
                    "id": event.id,
                    "kind": event.kind,
                    "level": event.level,
                    "message": _public_text(event.message),
                    "details": _public_value(event.details),
                    "created_at": event.created_at,
                }
                for event in events
            ],
            "next_after_id": events[-1].id if events else after_id,
        }
    )


@require_service_scopes("analysis:cancel")
@api_view(["POST"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_run_cancel(request, run_id):
    with transaction.atomic():
        run = get_object_or_404(
            _visible_runs(request).select_for_update(of=("self",)),
            pk=run_id,
        )
        if run.status in TERMINAL_STATUSES or run.status == AnalysisRun.Status.CANCEL_REQUESTED:
            return Response(integration_run_payload(run))
        if run.status == AnalysisRun.Status.QUEUED:
            run.status = AnalysisRun.Status.CANCELED
            run.current_step = "运行已取消"
            run.error = "运行已按请求取消。"
            run.finished_at = timezone.now()
            run.output_status = AnalysisRun.OutputStatus.UNAVAILABLE
            run.error_code = "ANALYSIS_CANCELED"
            run.error_category = "cancellation"
        else:
            run.status = AnalysisRun.Status.CANCEL_REQUESTED
            run.current_step = "正在取消运行"
        run.status_version += 1
        run.save()
        AnalysisRunEvent.objects.create(
            run=run,
            kind="cancellation",
            level="warning",
            message="外部调用方请求取消运行。",
            details={"status_version": run.status_version},
        )
    return Response(integration_run_payload(run))


@require_service_scopes("analysis:retry")
@api_view(["POST"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_run_retry(request, run_id):
    original = get_object_or_404(_visible_runs(request), pk=run_id)
    if original.status not in {AnalysisRun.Status.FAILED, AnalysisRun.Status.CANCELED}:
        return _error_response(
            request,
            IntegrationAPIError(
                "RETRY_SOURCE_NOT_TERMINAL",
                "只有失败或已取消的任务可以重跑。",
                category="validation",
                http_status=status.HTTP_409_CONFLICT,
            ),
        )
    try:
        body = dict(request.data)
        external = _external_ref(body.get("external_ref"))
        account = _request_service_account(request, external)
        if account.pk != original.service_account_id:
            raise IntegrationAPIError(
                "RETRY_CLIENT_MISMATCH",
                "重跑任务必须属于原 Service Account。",
                category="authorization",
                http_status=status.HTTP_403_FORBIDDEN,
            )
        external["client_id"] = account.client_id
        idempotency_key = _idempotency_key(request)
        canonical_body = {**body, "external_ref": external}
        digest = _canonical_digest(
            {
                "kind": "retry",
                "retry_of": str(original.id),
                "request": canonical_body,
            }
        )
        existing = _find_idempotent_run(
            account,
            external_run_id=external["external_run_id"],
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return _idempotent_response(request, existing, digest)
        if original.analysis_product_version_id:
            _validate_analysis_product_version_ready(
                original.analysis_product_version
            )
        try:
            _verify_run_resource_manifests(
                original,
                snapshot_budget=ResourceSnapshotBudget(),
            )
        except ResourceSnapshotBudgetError as error:
            raise IntegrationAPIError(
                "MANAGED_RESOURCE_LIMIT_EXCEEDED",
                str(error),
                category="resource",
                details={"retry_of": str(original.id)},
                http_status=status.HTTP_409_CONFLICT,
            ) from error
        except RuntimeError as error:
            failure = _failure_metadata(str(error))
            raise IntegrationAPIError(
                str(failure["code"]),
                str(error),
                category=str(failure["category"]),
                details={"retry_of": str(original.id)},
                http_status=status.HTTP_409_CONFLICT,
            ) from error
        payload = dict(original.request_payload)
        payload.update(
            {
                "external_ref": external,
                "attempt": int(original.request_payload.get("attempt") or 1) + 1,
                "retry_of": str(original.id),
                "retry_metadata": _validate_metadata(body.get("metadata")),
            }
        )
        try:
            with transaction.atomic():
                run = AnalysisRun.objects.create(
                    run_kind=original.run_kind,
                    workflow_version=original.workflow_version,
                    analysis_product_version=original.analysis_product_version,
                    tool_version=original.tool_version,
                    service_account=account,
                    external_run_id=external["external_run_id"],
                    external_analysis_id=external["external_analysis_id"],
                    idempotency_key=idempotency_key,
                    request_digest=digest,
                    retry_of=original,
                    workflow_name=original.workflow_name,
                    sample_id=original.sample_id,
                    sample_name=original.sample_name,
                    actor=_actor(request),
                    source_bundle=original.source_bundle,
                    source_digest=original.source_digest,
                    request_payload=payload,
                    input_values=original.input_values,
                )
                AnalysisRunEvent.objects.create(
                    run=run,
                    message="已基于原固定快照创建重跑任务。",
                    details={"retry_of": str(original.id), "status_version": 1},
                )
                AnalysisRunEvent.objects.create(
                    run=original,
                    kind="retry",
                    message="已创建新的重跑任务，原运行证据保持不变。",
                    details={"retry_run_id": str(run.id)},
                )
        except IntegrityError:
            run = _find_idempotent_run(
                account,
                external_run_id=external["external_run_id"],
                idempotency_key=idempotency_key,
            )
            if run is None:
                raise
            return _idempotent_response(request, run, digest)
    except IntegrationAPIError as error:
        return _error_response(request, error)
    return Response(integration_run_payload(run), status=status.HTTP_201_CREATED)


@require_service_scopes("analysis:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_run_outputs(request, run_id):
    run = get_object_or_404(_visible_runs(request), pk=run_id)
    return Response(
        {
            "run_id": str(run.id),
            "execution_status": run.status,
            "output_status": run.output_status,
            "error": _integration_error(run),
            "results": public_output_manifest(run),
        }
    )


@require_service_scopes("analysis:download")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_run_output_download(request, run_id):
    run = get_object_or_404(_visible_runs(request), pk=run_id)
    key = str(request.query_params.get("key") or "")
    manifest = run.output_manifest if isinstance(run.output_manifest, dict) else {}
    has_integrity_v2 = output_manifest_has_integrity_v2(manifest)
    items = manifest.get("items")
    if has_integrity_v2 and not isinstance(items, list):
        return _error_response(
            request,
            IntegrationAPIError(
                "ANALYSIS_OUTPUT_UNVERIFIED",
                "输出完整性清单无效，无法安全下载。",
                category="resource",
                http_status=status.HTTP_409_CONFLICT,
            ),
        )
    safe_items = items if isinstance(items, list) else []
    item = next(
        (
            value
            for value in safe_items
            if isinstance(value, dict) and value.get("key") == key
        ),
        None,
    )
    if not has_integrity_v2:
        legacy_output = next(
            (
                value
                for value in _output_payload(run)
                if value.get("kind") == "file" and value.get("key") == key
            ),
            None,
        )
        if legacy_output is not None:
            return _error_response(
                request,
                IntegrationAPIError(
                    "ANALYSIS_OUTPUT_UNVERIFIED",
                    "历史输出缺少完整性清单，无法安全下载。",
                    category="resource",
                    http_status=status.HTTP_409_CONFLICT,
                ),
            )
    if item is None:
        return _error_response(
            request,
            IntegrationAPIError(
                "ANALYSIS_OUTPUT_NOT_FOUND",
                "输出文件不存在。",
                category="application",
                http_status=status.HTTP_404_NOT_FOUND,
            ),
        )
    if not has_integrity_v2:
        return _error_response(
            request,
            IntegrationAPIError(
                "ANALYSIS_OUTPUT_UNVERIFIED",
                "历史输出缺少完整性清单，无法安全下载。",
                category="resource",
                http_status=status.HTTP_409_CONFLICT,
            ),
        )
    if item.get("kind") == "unverifiable":
        return _error_response(
            request,
            IntegrationAPIError(
                "ANALYSIS_OUTPUT_INCOMPLETE",
                "输出项未完成或完整性清单无效，无法安全下载。",
                category="resource",
                http_status=status.HTTP_409_CONFLICT,
            ),
        )
    if item.get("kind") != "file":
        return _error_response(
            request,
            IntegrationAPIError(
                "ANALYSIS_OUTPUT_NOT_FOUND",
                "输出文件不存在。",
                category="application",
                http_status=status.HTTP_404_NOT_FOUND,
            ),
        )
    if not output_manifest_file_item_is_verified(item):
        return _error_response(
            request,
            IntegrationAPIError(
                "ANALYSIS_OUTPUT_UNVERIFIED",
                "输出项完整性清单无效，无法安全下载。",
                category="resource",
                http_status=status.HTTP_409_CONFLICT,
            ),
        )
    try:
        path, handle = open_verified_output(
            item,
            run_root=run.work_directory,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return _error_response(
            request,
            IntegrationAPIError(
                "ANALYSIS_OUTPUT_CHANGED",
                "输出文件与成功时的不可变清单不一致。",
                category="resource",
                http_status=status.HTTP_409_CONFLICT,
            ),
        )
    response = FileResponse(
        handle,
        as_attachment=True,
        filename=str(item.get("filename") or path.name),
        content_type=str(item.get("content_type") or "application/octet-stream"),
    )
    response["ETag"] = f'"{str(item["sha256"]).removeprefix("sha256:")}"'
    return response


@require_service_scopes("analysis:read")
@api_view(["POST"])
@permission_classes([IntegrationScopePermission])
def integration_analysis_runs_batch_status(request):
    run_ids = request.data.get("run_ids") if isinstance(request.data, dict) else None
    if not isinstance(run_ids, list) or len(run_ids) > 200:
        return _error_response(
            request,
            IntegrationAPIError(
                "BATCH_STATUS_INVALID",
                "run_ids 必须是最多 200 项的数组。",
            ),
        )
    requested: set[str] = set()
    invalid: list[str] = []
    for value in run_ids:
        try:
            requested.add(str(uuid.UUID(str(value))))
        except (ValueError, AttributeError, TypeError):
            invalid.append(str(value))
    if invalid:
        return _error_response(
            request,
            IntegrationAPIError(
                "BATCH_STATUS_RUN_ID_INVALID",
                "run_ids 包含无效 UUID。",
                details={"run_ids": invalid},
            ),
        )
    runs = [run for run in _visible_runs(request).filter(pk__in=requested)]
    return Response(
        {
            "results": [
                {
                    "id": str(run.id),
                    "external_run_id": run.external_run_id,
                    "status": run.status,
                    "status_version": run.status_version,
                    "output_status": run.output_status,
                    "progress": run.progress,
                    "current_step": run.current_step,
                    "error": _integration_error(run),
                    "updated_at": run.updated_at,
                }
                for run in runs
            ],
            "missing": sorted(requested - {str(run.id) for run in runs}),
        }
    )


@require_service_scopes("task:test")
@api_view(["POST"])
@permission_classes([IntegrationScopePermission])
def integration_tool_test_preflight(request):
    try:
        result = _preflight_tool(dict(request.data))
    except IntegrationAPIError as error:
        return _error_response(request, error)
    item = result["tool_version"]
    return Response(
        {
            "ready": True,
            "tool": {
                "tool_id": item.tool_id,
                "version": item.version,
                "digest": item.digest,
                "name": item.name,
            },
            "checks": result["checks"],
            "resource_manifest": result["manifests"],
            "output_contract": result["output_contract"],
        }
    )


@require_service_scopes("task:test")
@api_view(["POST"])
@permission_classes([IntegrationScopePermission])
def integration_tool_test_runs(request):
    try:
        body = dict(request.data)
        external = _external_ref(body.get("external_ref"))
        account = _request_service_account(request, external)
        external["client_id"] = account.client_id
        idempotency_key = _idempotency_key(request)
        canonical_body = {**body, "external_ref": external}
        digest = _canonical_digest({"kind": "tool_test", "request": canonical_body})
        existing = _find_idempotent_run(
            account,
            external_run_id=external["external_run_id"],
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return _idempotent_response(request, existing, digest)
        preflight = _preflight_tool(body)
        item = preflight["tool_version"]
        sample_id = f"tool-test-{item.tool_id}"[:128]
        with transaction.atomic():
            run = AnalysisRun.objects.create(
                run_kind=AnalysisRun.Kind.TOOL_TEST,
                tool_version=item,
                service_account=account,
                external_run_id=external["external_run_id"],
                external_analysis_id=external["external_analysis_id"],
                idempotency_key=idempotency_key,
                request_digest=digest,
                workflow_name="tool_test",
                sample_id=sample_id,
                sample_name=str(body.get("label") or f"{item.name} 测试")[:256],
                actor=_actor(request),
                source_bundle=preflight["bundle"],
                source_digest=preflight["source_digest"],
                request_payload={
                    "kind": "integration_tool_test",
                    "attempt": 1,
                    "external_ref": external,
                    "managed_inputs": body.get("inputs") or {},
                    "tool_id": item.tool_id,
                    "tool_version": item.version,
                    "tool_digest": item.digest,
                    "integration_output_contract": preflight["output_contract"],
                    **preflight["manifests"],
                },
                input_values=preflight["input_values"],
            )
            AnalysisRunEvent.objects.create(
                run=run, message="外部工具测试已进入队列。"
            )
    except IntegrationAPIError as error:
        return _error_response(request, error)
    except IntegrityError:
        existing = _find_idempotent_run(
            account,
            external_run_id=external["external_run_id"],
            idempotency_key=idempotency_key,
        )
        if existing is None:
            raise
        return _idempotent_response(request, existing, digest)
    return Response(integration_run_payload(run), status=status.HTTP_201_CREATED)


@require_service_scopes("library:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_tools(request):
    tool_id = str(request.query_params.get("tool_id") or "").strip()
    versions = ToolVersion.objects.all()
    if tool_id:
        versions = versions.filter(tool_id=tool_id)
    return Response(
        {
            "results": [
                {
                    "tool_id": item.tool_id,
                    "version": item.version,
                    "name": item.name,
                    "digest": item.digest,
                    "description": item.tool_spec.get("description", ""),
                    "task_kind": item.tool_spec.get("task_kind", "standard"),
                    "inputs": item.tool_spec.get("inputs", []),
                    "outputs": item.tool_spec.get("outputs", []),
                    "runtime": item.tool_spec.get("runtime", {}),
                    "created_at": item.created_at,
                }
                for item in versions[:500]
            ]
        }
    )


@require_service_scopes("library:read")
@api_view(["GET"])
@permission_classes([IntegrationScopePermission])
def integration_software(request):
    items = SoftwareAsset.objects.prefetch_related("releases").all()
    return Response(
        {
            "results": [
                {
                    "slug": item.slug,
                    "name": item.name,
                    "summary": item.summary,
                    "description": item.description,
                    "homepage": item.homepage,
                    "source_repository": item.source_repository,
                    "license": item.license,
                    "notes": item.notes,
                    "tags": item.tags,
                    "lifecycle": item.lifecycle,
                    "releases": [
                        {
                            "version": release.version,
                            "description": release.description,
                            "container_images": release.container_images,
                        }
                        for release in item.releases.all()
                    ],
                }
                for item in items[:500]
            ]
        }
    )


@api_view(["GET"])
@permission_classes([])
def integration_openapi(request):
    path = Path(settings.BASE_DIR) / "schemas" / "integration-openapi-v1.json"
    return Response(json.loads(path.read_text(encoding="utf-8")))

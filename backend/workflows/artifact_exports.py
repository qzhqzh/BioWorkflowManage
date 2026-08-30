from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import signal
import stat as stat_module
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta, timezone as datetime_timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, Min, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .integration_outputs import (
    open_verified_output,
    output_manifest_file_item_is_verified,
    output_manifest_is_current,
    output_value_limit_reason,
)
from .models import (
    AnalysisOutputRetention,
    AnalysisRun,
    AnalysisRunEvent,
    ArtifactExport,
    ArtifactExportAttempt,
    ServiceAccount,
)
from .object_inputs import (
    ObjectInputError,
    _pin_client_connections,
    _validate_endpoint,
)


PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CLIENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BUCKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
SHA256_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
MAX_PROFILE_BYTES = 64 * 1024
MAX_ERROR_CHARS = 2000
S3_MINIMUM_MULTIPART_BYTES = 5 * 1024 * 1024
S3_MAXIMUM_MULTIPART_BYTES = 5 * 1024 * 1024 * 1024
S3_MAXIMUM_PARTS = 10_000
S3_MAXIMUM_OBJECT_BYTES = 5 * 1024 * 1024 * 1024 * 1024
ARTIFACT_EXPORT_EVENT_TYPE = "analysis.artifact_export.completed"
TERMINAL_RUN_STATES = {
    AnalysisRun.Status.SUCCEEDED,
    AnalysisRun.Status.FAILED,
    AnalysisRun.Status.CANCELED,
}


class ArtifactExportError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.http_status = http_status


class ArtifactExportLeaseLost(BaseException):
    pass


class _ArtifactExportDeadline(BaseException):
    pass


@dataclass(frozen=True, repr=False)
class ArtifactExportProfile:
    name: str
    kind: str
    allowed_clients: tuple[str, ...]
    directory: str = ""
    root_alias: str = ""
    public_uri_prefix: str = ""
    endpoint_url: str = ""
    region: str = ""
    bucket: str = ""
    prefix: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    allow_http: bool = False
    allow_private_network: bool = False
    allowed_networks: tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network, ...
    ] = ()
    expected_bucket_owner: str = ""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _profile_error(code: str, message: str) -> ArtifactExportError:
    return ArtifactExportError(code, message, http_status=503)


def _bounded_text(value: Any, *, maximum: int) -> str:
    result = str(value or "").strip()
    if len(result.encode("utf-8")) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in result
    ):
        raise ValueError("text exceeds its safe bound")
    return result


def _relative_directory(value: Any) -> str:
    raw = _bounded_text(value, maximum=512)
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("invalid relative directory")
    return "/".join(path.parts)


def _relative_prefix(value: Any) -> str:
    raw = _bounded_text(value, maximum=1024).strip("/")
    if not raw:
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid object prefix")
    return "/".join(path.parts)


def _read_profile(name: str) -> dict[str, Any]:
    if not PROFILE_PATTERN.fullmatch(name):
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_PROFILE_INVALID",
            "Artifact Export profile 名称无效。",
        )
    root = Path(settings.ANALYSIS_ARTIFACT_EXPORT_PROFILE_DIR)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise _profile_error(
            "ARTIFACT_EXPORT_PROFILE_NOT_FOUND",
            "Artifact Export profile 目录不存在。",
        ) from error
    path = resolved_root / f"{name}.json"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _profile_error(
            "ARTIFACT_EXPORT_PROFILE_NOT_FOUND",
            "Artifact Export profile 不存在或不可读取。",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PROFILE_BYTES:
            raise _profile_error(
                "ARTIFACT_EXPORT_PROFILE_INVALID",
                "Artifact Export profile 必须是受限大小的普通文件。",
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(MAX_PROFILE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise _profile_error(
            "ARTIFACT_EXPORT_PROFILE_INVALID",
            "Artifact Export profile 不是有效 UTF-8 JSON。",
        ) from error
    if not isinstance(value, dict):
        raise _profile_error(
            "ARTIFACT_EXPORT_PROFILE_INVALID",
            "Artifact Export profile 必须是 JSON object。",
        )
    return value


def _allowed_clients(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("allowed_clients must be a non-empty array")
    clients = tuple(str(item).strip() for item in value)
    if any(not CLIENT_PATTERN.fullmatch(item) for item in clients):
        raise ValueError("invalid allowed client")
    return clients


def load_artifact_export_profile(
    name: str,
    *,
    client_id: str,
) -> ArtifactExportProfile:
    value = _read_profile(name)
    try:
        kind = str(value.get("type") or "").strip()
        clients = _allowed_clients(value.get("allowed_clients"))
        if kind == "managed_directory":
            allowed = {
                "type",
                "allowed_clients",
                "directory",
                "root_alias",
                "public_uri_prefix",
            }
            if set(value) - allowed:
                raise ValueError("unknown managed profile fields")
            directory = _relative_directory(value.get("directory"))
            root_alias = _bounded_text(
                value.get("root_alias") or name,
                maximum=64,
            )
            if not PROFILE_PATTERN.fullmatch(root_alias):
                raise ValueError("invalid root alias")
            public_uri_prefix = _bounded_text(
                value.get("public_uri_prefix"),
                maximum=1024,
            ).rstrip("/")
            profile = ArtifactExportProfile(
                name=name,
                kind=kind,
                allowed_clients=clients,
                directory=directory,
                root_alias=root_alias,
                public_uri_prefix=public_uri_prefix,
            )
        elif kind == "s3":
            allowed = {
                "type",
                "allowed_clients",
                "endpoint_url",
                "region",
                "bucket",
                "prefix",
                "access_key_id",
                "secret_access_key",
                "session_token",
                "allow_http",
                "allow_private_network",
                "allowed_cidrs",
                "expected_bucket_owner",
            }
            if set(value) - allowed:
                raise ValueError("unknown S3 profile fields")
            endpoint_url = _bounded_text(value.get("endpoint_url"), maximum=2048)
            region = _bounded_text(value.get("region") or "us-east-1", maximum=128)
            bucket = _bounded_text(value.get("bucket"), maximum=63)
            if not endpoint_url or not region or not BUCKET_PATTERN.fullmatch(bucket):
                raise ValueError("invalid S3 destination")
            parsed_endpoint = urlsplit(endpoint_url)
            _ = parsed_endpoint.port
            if (
                parsed_endpoint.scheme not in {"http", "https"}
                or not parsed_endpoint.hostname
                or parsed_endpoint.username is not None
                or parsed_endpoint.password is not None
                or parsed_endpoint.query
                or parsed_endpoint.fragment
                or ".." in Path(parsed_endpoint.path).parts
            ):
                raise ValueError("invalid S3 endpoint")
            parsed_endpoint.hostname.encode("ascii")
            if parsed_endpoint.scheme == "http" and value.get("allow_http") is not True:
                raise ValueError("HTTP endpoint is not explicitly allowed")
            access_key_id = str(value.get("access_key_id") or "")
            secret_access_key = str(value.get("secret_access_key") or "")
            session_token = str(value.get("session_token") or "")
            if not access_key_id or not secret_access_key:
                raise ValueError("missing S3 credentials")
            raw_networks = value.get("allowed_cidrs") or []
            if not isinstance(raw_networks, list):
                raise ValueError("allowed_cidrs must be an array")
            networks = tuple(
                ipaddress.ip_network(str(item), strict=False) for item in raw_networks
            )
            profile = ArtifactExportProfile(
                name=name,
                kind=kind,
                allowed_clients=clients,
                endpoint_url=endpoint_url,
                region=region,
                bucket=bucket,
                prefix=_relative_prefix(value.get("prefix")),
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                session_token=session_token,
                allow_http=value.get("allow_http") is True,
                allow_private_network=value.get("allow_private_network") is True,
                allowed_networks=networks,
                expected_bucket_owner=_bounded_text(
                    value.get("expected_bucket_owner"),
                    maximum=64,
                ),
            )
        else:
            raise ValueError("unknown profile type")
    except (TypeError, ValueError) as error:
        raise _profile_error(
            "ARTIFACT_EXPORT_PROFILE_INVALID",
            "Artifact Export profile 配置无效。",
        ) from error
    if client_id not in profile.allowed_clients:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_PROFILE_FORBIDDEN",
            "当前 Service Account 无权使用该 Artifact Export profile。",
            http_status=403,
        )
    return profile


def _profile_routing(profile: ArtifactExportProfile) -> dict[str, Any]:
    if profile.kind == "managed_directory":
        return {
            "type": profile.kind,
            "directory": profile.directory,
            "root_alias": profile.root_alias,
            "public_uri_prefix": profile.public_uri_prefix,
        }
    parsed = urlsplit(profile.endpoint_url)
    origin = f"{parsed.scheme}://{parsed.hostname or ''}"
    if parsed.port is not None:
        origin += f":{parsed.port}"
    return {
        "type": profile.kind,
        "endpoint_origin": origin,
        "endpoint_path": parsed.path or "/",
        "region": profile.region,
        "bucket": profile.bucket,
        "prefix": profile.prefix,
        "allow_http": profile.allow_http,
        "allow_private_network": profile.allow_private_network,
        "allowed_cidrs": [str(item) for item in profile.allowed_networks],
        "expected_bucket_owner": profile.expected_bucket_owner,
    }


def artifact_export_target_snapshot(profile: ArtifactExportProfile) -> dict[str, Any]:
    routing = _profile_routing(profile)
    if profile.kind == "managed_directory":
        public = {
            "type": profile.kind,
            "profile": profile.name,
            "root_alias": profile.root_alias,
        }
        if profile.public_uri_prefix:
            public["public_uri_prefix"] = profile.public_uri_prefix
    else:
        public = {
            "type": profile.kind,
            "profile": profile.name,
            "endpoint_origin": routing["endpoint_origin"],
            "bucket": profile.bucket,
            "prefix": profile.prefix,
        }
    public["routing_digest"] = _digest(routing)
    return public


def _validate_profile_snapshot(
    export: ArtifactExport,
    profile: ArtifactExportProfile,
) -> None:
    current = artifact_export_target_snapshot(profile)
    expected = export.target_snapshot if isinstance(export.target_snapshot, dict) else {}
    if current != expected:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_TARGET_CHANGED",
            "Artifact Export profile 的目标路由已变化；拒绝把重试写入其他位置。",
        )


def _source_items(run: AnalysisRun) -> list[dict[str, Any]]:
    manifest = run.output_manifest if isinstance(run.output_manifest, dict) else {}
    if (
        run.output_status != AnalysisRun.OutputStatus.COMPLETE
        or not output_manifest_is_current(manifest)
    ):
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_OUTPUT_UNVERIFIED",
            "分析输出缺少完整且可验证的 v2 清单。",
            http_status=409,
        )
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_OUTPUT_EMPTY",
            "分析运行没有可导出的结果。",
            http_status=409,
        )
    for item in items:
        if not isinstance(item, dict):
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_OUTPUT_UNVERIFIED",
                "分析输出清单包含无效条目。",
                http_status=409,
            )
        if item.get("kind") == "directory":
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_DIRECTORY_UNSUPPORTED",
                "当前 Artifact Export 契约仅交付 File 与有界 JSON value；Directory 输出需先由流程打包。",
                http_status=409,
            )
        if item.get("kind") == "file" and not output_manifest_file_item_is_verified(item):
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_OUTPUT_UNVERIFIED",
                "分析输出文件缺少不可变证据。",
                http_status=409,
            )
        if item.get("kind") == "value" and output_value_limit_reason(item.get("value")):
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_OUTPUT_UNVERIFIED",
                "分析输出 value 超过安全边界。",
                http_status=409,
            )
    if len(_canonical_bytes(manifest)) > int(
        settings.ANALYSIS_ARTIFACT_EXPORT_MANIFEST_MAX_BYTES
    ):
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_MANIFEST_TOO_LARGE",
            "分析输出清单超过 Artifact Export 大小上限。",
            http_status=413,
        )
    return items


def _requested_retention(value: Any) -> tuple[str | None, Any]:
    if value in (None, ""):
        return None, None
    if not isinstance(value, str):
        raise ArtifactExportError(
            "ARTIFACT_RETENTION_INVALID",
            "retain_until 必须是带时区的 ISO-8601 时间。",
        )
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ArtifactExportError(
            "ARTIFACT_RETENTION_INVALID",
            "retain_until 必须是带时区的 ISO-8601 时间。",
        )
    normalized = parsed.astimezone(datetime_timezone.utc).isoformat()
    return normalized, parsed


def create_artifact_export(
    *,
    run: AnalysisRun,
    account: ServiceAccount,
    idempotency_key: str,
    profile_name: str,
    requires_ack: bool,
    retain_until: Any,
    actor: str,
) -> tuple[ArtifactExport, bool]:
    normalized_retention, requested_retention = _requested_retention(retain_until)
    request_digest = _digest(
        {
            "kind": "artifact_export",
            "run_id": str(run.id),
            "profile": profile_name,
            "requires_ack": requires_ack,
            "retain_until": normalized_retention,
        }
    )
    existing = ArtifactExport.objects.filter(
        service_account=account,
        idempotency_key=idempotency_key,
    ).first()
    if existing is not None:
        if existing.request_digest != request_digest or existing.run_id != run.id:
            raise ArtifactExportError(
                "IDEMPOTENCY_CONFLICT",
                "该 Idempotency-Key 已用于不同的 Artifact Export 请求。",
                http_status=409,
            )
        return existing, False

    profile = load_artifact_export_profile(
        profile_name,
        client_id=account.client_id,
    )
    target_snapshot = artifact_export_target_snapshot(profile)
    now = timezone.now()
    minimum = now + timedelta(days=int(settings.ANALYSIS_ARTIFACT_RETENTION_MIN_DAYS))
    maximum = now + timedelta(days=int(settings.ANALYSIS_ARTIFACT_RETENTION_MAX_DAYS))
    if requested_retention is not None and requested_retention > maximum:
        raise ArtifactExportError(
            "ARTIFACT_RETENTION_INVALID",
            "retain_until 超过部署允许的最长保留期。",
        )
    effective_retention = max(minimum, requested_retention or minimum)

    try:
        with transaction.atomic():
            existing = ArtifactExport.objects.select_for_update().filter(
                service_account=account,
                idempotency_key=idempotency_key,
            ).first()
            if existing is not None:
                if existing.request_digest != request_digest or existing.run_id != run.id:
                    raise ArtifactExportError(
                        "IDEMPOTENCY_CONFLICT",
                        "该 Idempotency-Key 已用于不同的 Artifact Export 请求。",
                        http_status=409,
                    )
                return existing, False
            locked_run = (
                AnalysisRun.objects.select_for_update()
                .select_related("service_account")
                .get(pk=run.pk)
            )
            if locked_run.service_account_id != account.id:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_FORBIDDEN",
                    "当前 Service Account 无权导出该分析结果。",
                    http_status=403,
                )
            if locked_run.status != AnalysisRun.Status.SUCCEEDED:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_RUN_NOT_READY",
                    "只有执行成功且输出已固化的运行可以创建 Artifact Export。",
                    http_status=409,
                )
            if not locked_run.external_run_id:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_EXTERNAL_REF_MISSING",
                    "Artifact Export 需要稳定的外部运行 ID。",
                    http_status=409,
                )
            _source_items(locked_run)
            source_manifest_digest = _digest(locked_run.output_manifest)
            retention = AnalysisOutputRetention.objects.select_for_update().filter(
                run=locked_run
            ).first()
            if retention is None:
                retention = AnalysisOutputRetention.objects.create(
                    run=locked_run,
                    retain_until=effective_retention,
                    created_by=actor[:256],
                )
            elif (
                retention.state
                in {
                    AnalysisOutputRetention.State.CLEANING,
                    AnalysisOutputRetention.State.CLEANED,
                }
                or retention.quarantined_at is not None
            ):
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_OUTPUT_CLEANING",
                    "分析输出正在清理或已经清理，不能创建新导出。",
                    http_status=409,
                )
            elif effective_retention > retention.retain_until:
                retention.retain_until = effective_retention
                retention.state = AnalysisOutputRetention.State.PROTECTED
                retention.last_error_code = ""
                retention.last_error = ""
                retention.save(
                    update_fields=[
                        "retain_until",
                        "state",
                        "last_error_code",
                        "last_error",
                        "updated_at",
                    ]
                )
            export = ArtifactExport.objects.create(
                run=locked_run,
                service_account=account,
                retention=retention,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                source_manifest_digest=source_manifest_digest,
                target_profile=profile.name,
                target_snapshot=target_snapshot,
                requires_ack=requires_ack,
            )
            AnalysisRunEvent.objects.create(
                run=locked_run,
                kind="artifact_export",
                message="已创建异步 Artifact Export。",
                details={
                    "artifact_export_id": str(export.id),
                    "target_profile": profile.name,
                    "requires_ack": requires_ack,
                    "retain_until": retention.retain_until.isoformat(),
                },
            )
    except IntegrityError:
        existing = ArtifactExport.objects.filter(
            service_account=account,
            idempotency_key=idempotency_key,
        ).first()
        if existing is None:
            raise
        if existing.request_digest != request_digest or existing.run_id != run.id:
            raise ArtifactExportError(
                "IDEMPOTENCY_CONFLICT",
                "该 Idempotency-Key 已用于不同的 Artifact Export 请求。",
                http_status=409,
            )
        return existing, False
    return export, True


def _retention_blockers(
    retention: AnalysisOutputRetention,
    *,
    now=None,
) -> list[str]:
    now = now or timezone.now()
    blockers: list[str] = []
    if retention.state == AnalysisOutputRetention.State.CLEANED:
        return ["already_cleaned"]
    if retention.state == AnalysisOutputRetention.State.CLEANING:
        blockers.append("cleanup_in_progress")
    if retention.retain_until > now:
        blockers.append("retention_period_active")
    run = retention.run
    if run.status not in TERMINAL_RUN_STATES:
        blockers.append("run_not_terminal")
    if (
        retention.state
        in {
            AnalysisOutputRetention.State.PROTECTED,
            AnalysisOutputRetention.State.FAILED,
        }
        and retention.quarantined_at is None
        and run.output_status != AnalysisRun.OutputStatus.COMPLETE
    ):
        blockers.append("local_output_unavailable")
    exports = list(retention.artifact_exports.all())
    if not exports:
        blockers.append("no_artifact_export")
    if any(item.state != ArtifactExport.State.SUCCEEDED for item in exports):
        blockers.append("artifact_export_incomplete")
    if any(item.requires_ack and item.acknowledged_at is None for item in exports):
        blockers.append("delivery_unacknowledged")
    return list(dict.fromkeys(blockers))


def artifact_export_payload(
    export: ArtifactExport,
    *,
    include_manifest: bool = True,
) -> dict[str, Any]:
    retention = export.retention
    target = dict(export.target_snapshot) if isinstance(export.target_snapshot, dict) else {}
    target.pop("routing_digest", None)
    payload = {
        "id": str(export.id),
        "run_id": str(export.run_id),
        "state": export.state,
        "target": target,
        "source_manifest_digest": export.source_manifest_digest,
        "manifest_digest": export.manifest_digest or None,
        "manifest_location": (
            export.manifest_location
            if export.state == ArtifactExport.State.SUCCEEDED
            and isinstance(export.manifest_location, dict)
            else None
        ),
        "manifest": (
            export.manifest
            if export.state == ArtifactExport.State.SUCCEEDED
            and include_manifest
            and isinstance(export.manifest, dict)
            else None
        ),
        "requires_ack": export.requires_ack,
        "acknowledged_at": (
            export.acknowledged_at.isoformat() if export.acknowledged_at else None
        ),
        "attempt_count": export.attempt_count,
        "replay_count": export.replay_count,
        "next_attempt_at": (
            export.next_attempt_at.isoformat()
            if export.state == ArtifactExport.State.PENDING
            else None
        ),
        "completed_at": export.completed_at.isoformat() if export.completed_at else None,
        "error": (
            {
                "code": export.last_error_code,
                "message": export.last_error,
                "retryable": export.last_error_retryable,
            }
            if export.last_error_code
            else None
        ),
        "retention": {
            "state": retention.state,
            "retain_until": retention.retain_until.isoformat(),
            "cleanup_eligible": not _retention_blockers(retention),
            "cleanup_blockers": _retention_blockers(retention),
            "quarantined_at": (
                retention.quarantined_at.isoformat()
                if retention.quarantined_at
                else None
            ),
            "cleaned_at": retention.cleaned_at.isoformat() if retention.cleaned_at else None,
        },
        "created_at": export.created_at.isoformat(),
        "updated_at": export.updated_at.isoformat(),
        "links": {
            "self": f"/api/v1/integration/artifact-exports/{export.id}",
            "acknowledge": (
                f"/api/v1/integration/artifact-exports/{export.id}/acknowledge"
            ),
            "run": f"/api/v1/integration/analysis-runs/{export.run_id}",
        },
    }
    return payload


def acknowledge_artifact_export(
    export: ArtifactExport,
    *,
    manifest_digest: str,
    external_receipt: str,
    actor: str,
) -> ArtifactExport:
    if not SHA256_PATTERN.fullmatch(manifest_digest):
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_ACK_INVALID",
            "manifest_digest 必须是 Artifact Export 返回的 SHA-256。",
        )
    try:
        receipt = _bounded_text(external_receipt, maximum=128)
    except ValueError as error:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_ACK_INVALID",
            "external_receipt 超过安全长度或包含控制字符。",
        ) from error
    with transaction.atomic():
        current = (
            ArtifactExport.objects.select_for_update()
            .select_related("run", "retention")
            .get(pk=export.pk)
        )
        if current.state != ArtifactExport.State.SUCCEEDED:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_NOT_DELIVERED",
                "Artifact Export 尚未成功，不能确认交付。",
                http_status=409,
            )
        if current.manifest_digest != manifest_digest:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_ACK_DIGEST_MISMATCH",
                "确认的 manifest_digest 与已交付清单不一致。",
                http_status=409,
            )
        acknowledgement = {
            "manifest_digest": manifest_digest,
            "external_receipt": receipt,
        }
        if current.acknowledged_at is not None:
            if current.acknowledgement != acknowledgement:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_ACK_CONFLICT",
                    "Artifact Export 已使用不同的回执确认。",
                    http_status=409,
                )
            return current
        current.acknowledged_at = timezone.now()
        current.acknowledged_by = actor[:256]
        current.acknowledgement = acknowledgement
        current.save(
            update_fields=[
                "acknowledged_at",
                "acknowledged_by",
                "acknowledgement",
                "updated_at",
            ]
        )
        AnalysisRunEvent.objects.create(
            run=current.run,
            kind="artifact_export",
            message="外部系统已确认 Artifact Export 交付。",
            details={
                "artifact_export_id": str(current.id),
                "manifest_digest": manifest_digest,
                "external_receipt": receipt,
            },
        )
        return current


def artifact_export_deadline_supported() -> bool:
    return hasattr(signal, "setitimer") and threading.current_thread() is (
        threading.main_thread()
    )


@contextmanager
def _artifact_export_wall_clock_timeout(seconds: float) -> Iterator[None]:
    if not artifact_export_deadline_supported():
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_DEADLINE_UNSUPPORTED",
            "artifact-exporter 必须在支持 POSIX wall-clock timer 的主线程运行。",
            retryable=True,
            http_status=503,
        )
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer[0] > 0 or previous_timer[1] > 0:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_TIMER_CONFLICT",
            "artifact-exporter 检测到已有 wall-clock timer，拒绝覆盖。",
            retryable=True,
            http_status=503,
        )
    previous_handler = signal.getsignal(signal.SIGALRM)

    def deadline_exceeded(_signum, _frame) -> None:
        raise _ArtifactExportDeadline()

    signal.signal(signal.SIGALRM, deadline_exceeded)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, float(seconds)))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _retry_delay(attempt_count: int) -> timedelta:
    base = max(1.0, float(settings.ANALYSIS_ARTIFACT_EXPORT_BACKOFF_BASE_SECONDS))
    maximum = max(base, float(settings.ANALYSIS_ARTIFACT_EXPORT_BACKOFF_MAX_SECONDS))
    seconds = min(maximum, base * (2 ** max(0, attempt_count - 1)))
    return timedelta(seconds=seconds)


def _recover_expired_exports(now) -> None:
    expired = list(
        ArtifactExport.objects.select_for_update(skip_locked=True, of=("self",))
        .filter(
            state=ArtifactExport.State.EXPORTING,
            lease_expires_at__lt=now,
        )
        .order_by("lease_expires_at")[:50]
    )
    maximum = max(1, int(settings.ANALYSIS_ARTIFACT_EXPORT_MAX_ATTEMPTS))
    for export in expired:
        attempt = export.attempts.filter(finished_at__isnull=True).order_by(
            "-attempt_number"
        ).first()
        if attempt is not None:
            attempt.outcome = ArtifactExportAttempt.Outcome.LEASE_EXPIRED
            attempt.error_code = "ARTIFACT_EXPORT_LEASE_EXPIRED"
            attempt.error = "artifact-exporter 租约过期；目标端可能保留可幂等复用的部分对象。"
            attempt.finished_at = now
            attempt.save(
                update_fields=["outcome", "error_code", "error", "finished_at"]
            )
        export.last_error_code = "ARTIFACT_EXPORT_LEASE_EXPIRED"
        export.last_error = "artifact-exporter 租约过期。"
        export.last_error_retryable = True
        export.lease_token = None
        export.lease_expires_at = None
        if export.attempt_count >= maximum:
            export.state = ArtifactExport.State.DEAD_LETTER
        else:
            export.state = ArtifactExport.State.PENDING
            export.next_attempt_at = now
        export.save()


def _dead_letter_inactive_exports(now) -> None:
    inactive = list(
        ArtifactExport.objects.select_for_update(skip_locked=True, of=("self",))
        .select_related("service_account")
        .filter(
            state=ArtifactExport.State.PENDING,
            next_attempt_at__lte=now,
            service_account__is_active=False,
        )
        .order_by("next_attempt_at")[:50]
    )
    for export in inactive:
        export.state = ArtifactExport.State.DEAD_LETTER
        export.last_error_code = "ARTIFACT_EXPORT_SERVICE_ACCOUNT_INACTIVE"
        export.last_error = "Service Account 已停用。"
        export.last_error_retryable = False
        export.save(
            update_fields=[
                "state",
                "last_error_code",
                "last_error",
                "last_error_retryable",
                "updated_at",
            ]
        )


def claim_next_artifact_export() -> ArtifactExport | None:
    with transaction.atomic():
        now = timezone.now()
        _recover_expired_exports(now)
        _dead_letter_inactive_exports(now)
        export = (
            ArtifactExport.objects.select_for_update(
                skip_locked=True,
                of=("self",),
            )
            .select_related("run", "service_account", "retention")
            .filter(
                state=ArtifactExport.State.PENDING,
                next_attempt_at__lte=now,
                service_account__is_active=True,
            )
            .order_by("next_attempt_at", "created_at")
            .first()
        )
        if export is None:
            return None
        export.state = ArtifactExport.State.EXPORTING
        export.attempt_count += 1
        export.lease_token = uuid.uuid4()
        export.lease_expires_at = now + timedelta(
            seconds=int(settings.ANALYSIS_ARTIFACT_EXPORT_LEASE_SECONDS)
        )
        export.save(
            update_fields=[
                "state",
                "attempt_count",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )
        ArtifactExportAttempt.objects.create(
            export=export,
            attempt_number=export.attempt_count,
            replay_number=export.replay_count,
            files_total=sum(
                item.get("kind") == "file"
                for item in (export.run.output_manifest or {}).get("items", [])
                if isinstance(item, dict)
            ),
        )
        return export


def _renew_export_lease(export: ArtifactExport, *, force: bool = False) -> None:
    if export.lease_token is None:
        raise ArtifactExportLeaseLost()
    interval = min(
        30.0,
        max(1.0, float(settings.ANALYSIS_ARTIFACT_EXPORT_LEASE_SECONDS) / 3),
    )
    monotonic_now = time.monotonic()
    last_renewed = float(getattr(export, "_lease_renewed_monotonic", 0.0))
    if not force and monotonic_now - last_renewed < interval:
        return
    now = timezone.now()
    updated = ArtifactExport.objects.filter(
        pk=export.pk,
        state=ArtifactExport.State.EXPORTING,
        lease_token=export.lease_token,
        lease_expires_at__gte=now,
        service_account__is_active=True,
    ).update(
        lease_expires_at=now
        + timedelta(seconds=int(settings.ANALYSIS_ARTIFACT_EXPORT_LEASE_SECONDS)),
        updated_at=now,
    )
    if updated != 1:
        raise ArtifactExportLeaseLost()
    export._lease_renewed_monotonic = monotonic_now


def _safe_filename(value: Any) -> str:
    name = Path(str(value or "artifact.bin")).name
    encoded = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (encoded or "artifact.bin")[:128]


def _item_destination_name(index: int, item: dict[str, Any]) -> str:
    match = SHA256_PATTERN.fullmatch(str(item.get("sha256") or ""))
    if match is None:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_OUTPUT_UNVERIFIED",
            "分析输出文件缺少有效 SHA-256。",
        )
    return f"{index:04d}-{match.group(1)[:16]}-{_safe_filename(item.get('filename'))}"


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_or_create_directory(parent: int, name: str, *, mode: int = 0o700) -> int:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_PATH_INVALID",
            "Artifact Export 目标目录无效。",
        )
    created = False
    try:
        os.mkdir(name, mode=mode, dir_fd=parent)
        created = True
    except FileExistsError:
        pass
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    except OSError as error:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_PATH_UNSAFE",
            "Artifact Export 目标目录不是安全的普通目录。",
        ) from error
    if not stat_module.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_PATH_UNSAFE",
            "Artifact Export 目标目录不是普通目录。",
        )
    if created:
        try:
            os.fchmod(descriptor, mode)
        except OSError:
            os.close(descriptor)
            raise
    return descriptor


@contextmanager
def _managed_export_directory(
    profile: ArtifactExportProfile,
    export: ArtifactExport,
) -> Iterator[tuple[int, str]]:
    root = Path(settings.ANALYSIS_ARTIFACT_EXPORT_ROOT)
    if root.resolve(strict=False) == Path("/"):
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_PATH_UNSAFE",
            "Artifact Export 根目录不能是文件系统根目录。",
        )
    try:
        root.mkdir(parents=True, exist_ok=True)
        root_descriptor = os.open(root, _directory_flags())
    except OSError as error:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_DESTINATION_UNAVAILABLE",
            "受管 Artifact Export 根目录不可用。",
            retryable=True,
            http_status=503,
        ) from error
    descriptors = [root_descriptor]
    components = [*PurePosixPath(profile.directory).parts, str(export.run_id), str(export.id)]
    try:
        current = root_descriptor
        for component in components:
            current = _open_or_create_directory(current, component, mode=0o750)
            descriptors.append(current)
        yield current, "/".join(components)
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _verify_managed_existing(
    directory: int,
    name: str,
    *,
    expected_size: int,
    expected_sha256: str,
    export: ArtifactExport,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except OSError as error:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_DESTINATION_CONFLICT",
            "受管导出目标已存在但无法验证。",
        ) from error
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat_module.S_ISREG(before.st_mode) or before.st_mode & 0o222:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_DESTINATION_CONFLICT",
                "受管导出目标已存在但不是只读普通文件。",
            )
        while chunk := os.read(
            descriptor,
            int(settings.ANALYSIS_ARTIFACT_EXPORT_CHUNK_BYTES),
        ):
            digest.update(chunk)
            size += len(chunk)
            _renew_export_lease(export)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    actual = "sha256:" + digest.hexdigest()
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or size != expected_size
        or actual != expected_sha256
    ):
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_DESTINATION_CONFLICT",
            "受管导出目标已存在且内容与不可变清单不一致。",
        )


def _managed_location(
    profile: ArtifactExportProfile,
    *,
    relative_path: str,
    size: int,
    sha256: str,
) -> dict[str, Any]:
    location: dict[str, Any] = {
        "type": "managed_directory",
        "profile": profile.name,
        "root_alias": profile.root_alias,
        "relative_path": relative_path,
        "size": size,
        "sha256": sha256,
    }
    if profile.public_uri_prefix:
        location["uri"] = f"{profile.public_uri_prefix}/{relative_path}"
    else:
        location["uri"] = f"managed://{profile.root_alias}/{relative_path}"
    return location


def _write_managed_stream(
    directory: int,
    base_relative: str,
    profile: ArtifactExportProfile,
    export: ArtifactExport,
    *,
    name: str,
    source: BinaryIO,
    expected_size: int,
    expected_sha256: str,
) -> dict[str, Any]:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        _verify_managed_existing(
            directory,
            name,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            export=export,
        )
        return _managed_location(
            profile,
            relative_path=f"{base_relative}/{name}",
            size=expected_size,
            sha256=expected_sha256,
        )

    temporary_name = f".part-{export.id}-{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    published = False
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
        source.seek(0)
        while chunk := source.read(int(settings.ANALYSIS_ARTIFACT_EXPORT_CHUNK_BYTES)):
            digest.update(chunk)
            size += len(chunk)
            _write_all(descriptor, chunk)
            _renew_export_lease(export)
        actual = "sha256:" + digest.hexdigest()
        if size != expected_size or actual != expected_sha256:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_SOURCE_CHANGED",
                "分析输出在 Artifact Export 期间发生变化。",
            )
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError:
            _verify_managed_existing(
                directory,
                name,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                export=export,
            )
        os.fsync(directory)
    except OSError as error:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_DESTINATION_UNAVAILABLE",
            "写入受管 Artifact Export 目标失败。",
            retryable=True,
            http_status=503,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        if published:
            try:
                os.fsync(directory)
            except OSError:
                pass
    return _managed_location(
        profile,
        relative_path=f"{base_relative}/{name}",
        size=expected_size,
        sha256=expected_sha256,
    )


def _s3_client(profile: ArtifactExportProfile):
    try:
        addresses = _validate_endpoint(profile)
    except ObjectInputError as error:
        if error.retryable:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_ENDPOINT_UNAVAILABLE",
                "Artifact Export endpoint 暂时无法解析或访问。",
                retryable=True,
                http_status=503,
            ) from error
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_ENDPOINT_FORBIDDEN",
            "Artifact Export endpoint 未通过部署网络策略。",
            http_status=403,
        ) from error
    timeout = min(60.0, max(1.0, float(settings.ANALYSIS_ARTIFACT_EXPORT_TIMEOUT_SECONDS)))
    client = boto3.client(
        "s3",
        endpoint_url=profile.endpoint_url,
        region_name=profile.region,
        aws_access_key_id=profile.access_key_id,
        aws_secret_access_key=profile.secret_access_key,
        aws_session_token=profile.session_token or None,
        config=Config(
            signature_version="s3v4",
            connect_timeout=min(10.0, timeout),
            read_timeout=timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
            proxies={},
        ),
    )
    try:
        return _pin_client_connections(client, profile, addresses)
    except BaseException:
        close = getattr(client, "close", None)
        if callable(close):
            close()
        raise


def _s3_parameters(profile: ArtifactExportProfile, key: str) -> dict[str, Any]:
    parameters: dict[str, Any] = {"Bucket": profile.bucket, "Key": key}
    if profile.expected_bucket_owner:
        parameters["ExpectedBucketOwner"] = profile.expected_bucket_owner
    return parameters


def _mapped_s3_error(error: BaseException) -> ArtifactExportError:
    if isinstance(error, ArtifactExportError):
        return error
    if isinstance(error, ClientError):
        response = error.response if isinstance(error.response, dict) else {}
        metadata = response.get("ResponseMetadata") or {}
        status_code = int(metadata.get("HTTPStatusCode") or 0)
        code = str((response.get("Error") or {}).get("Code") or "")
        if status_code in {401, 403} or code in {
            "AccessDenied",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
        }:
            return ArtifactExportError(
                "ARTIFACT_EXPORT_DESTINATION_FORBIDDEN",
                "Artifact Export 目标拒绝写入。",
                http_status=403,
            )
        if status_code == 404 or code in {"NoSuchBucket"}:
            return ArtifactExportError(
                "ARTIFACT_EXPORT_DESTINATION_NOT_FOUND",
                "Artifact Export bucket 不存在。",
                http_status=404,
            )
        return ArtifactExportError(
            "ARTIFACT_EXPORT_DESTINATION_UNAVAILABLE",
            "Artifact Export 对象存储暂时不可用。",
            retryable=status_code >= 500 or status_code == 429 or status_code == 0,
            http_status=503,
        )
    if isinstance(error, (BotoCoreError, HTTPClientError, OSError, TimeoutError)):
        return ArtifactExportError(
            "ARTIFACT_EXPORT_DESTINATION_UNAVAILABLE",
            "Artifact Export 对象存储暂时不可用。",
            retryable=True,
            http_status=503,
        )
    return ArtifactExportError(
        "ARTIFACT_EXPORT_FAILED",
        "Artifact Export 执行失败。",
        retryable=True,
        http_status=503,
    )


def _s3_head(client, profile: ArtifactExportProfile, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(**_s3_parameters(profile, key))
    except ClientError as error:
        response = error.response if isinstance(error.response, dict) else {}
        status_code = int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)
        code = str((response.get("Error") or {}).get("Code") or "")
        if status_code == 404 or code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise _mapped_s3_error(error) from error
    except (BotoCoreError, HTTPClientError, OSError, TimeoutError) as error:
        raise _mapped_s3_error(error) from error


def _s3_location(
    profile: ArtifactExportProfile,
    key: str,
    response: dict[str, Any],
    *,
    size: int,
    sha256: str,
) -> dict[str, Any]:
    location: dict[str, Any] = {
        "type": "s3_object",
        "profile": profile.name,
        "bucket": profile.bucket,
        "key": key,
        "uri": f"s3://{profile.bucket}/{key}",
        "size": size,
        "sha256": sha256,
    }
    etag = str(response.get("ETag") or "").strip('"')
    version_id = str(response.get("VersionId") or "").strip()
    if etag:
        location["etag"] = etag
    if version_id:
        location["version_id"] = version_id
    return location


def _verify_s3_existing(
    profile: ArtifactExportProfile,
    key: str,
    response: dict[str, Any],
    *,
    expected_size: int,
    expected_sha256: str,
) -> dict[str, Any]:
    metadata = response.get("Metadata") or {}
    actual_sha256 = str(metadata.get("sha256") or "")
    if actual_sha256 and not actual_sha256.startswith("sha256:"):
        actual_sha256 = f"sha256:{actual_sha256}"
    if response.get("ContentLength") != expected_size or actual_sha256 != expected_sha256:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_DESTINATION_CONFLICT",
            "Artifact Export 目标对象已存在且内容证据不一致。",
        )
    return _s3_location(
        profile,
        key,
        response,
        size=expected_size,
        sha256=expected_sha256,
    )


def _write_s3_stream(
    client,
    profile: ArtifactExportProfile,
    export: ArtifactExport,
    *,
    key: str,
    source: BinaryIO,
    expected_size: int,
    expected_sha256: str,
    content_type: str,
) -> dict[str, Any]:
    existing = _s3_head(client, profile, key)
    if existing is not None:
        return _verify_s3_existing(
            profile,
            key,
            existing,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
    digest = hashlib.sha256()
    size = 0
    upload_id = ""
    try:
        source.seek(0)
        part_size = _s3_part_size(expected_size)
        metadata = {"sha256": expected_sha256.removeprefix("sha256:")}
        parameters = _s3_parameters(profile, key)
        if expected_size <= part_size:
            value = source.read(part_size + 1)
            digest.update(value)
            size = len(value)
            if size > part_size:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_SOURCE_CHANGED",
                    "分析输出大小与不可变清单不一致。",
                )
            actual = "sha256:" + digest.hexdigest()
            if size != expected_size or actual != expected_sha256:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_SOURCE_CHANGED",
                    "分析输出在 Artifact Export 期间发生变化。",
                )
            client.put_object(
                **parameters,
                Body=value,
                ContentLength=size,
                ContentType=content_type,
                Metadata=metadata,
            )
            _renew_export_lease(export)
        else:
            created = client.create_multipart_upload(
                **parameters,
                ContentType=content_type,
                Metadata=metadata,
            )
            upload_id = str(created["UploadId"])
            parts = []
            part_number = 1
            while value := source.read(part_size):
                digest.update(value)
                size += len(value)
                uploaded = client.upload_part(
                    **parameters,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=value,
                    ContentLength=len(value),
                )
                parts.append({"ETag": uploaded["ETag"], "PartNumber": part_number})
                part_number += 1
                _renew_export_lease(export)
            actual = "sha256:" + digest.hexdigest()
            if size != expected_size or actual != expected_sha256:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_SOURCE_CHANGED",
                    "分析输出在 Artifact Export 期间发生变化。",
                )
            client.complete_multipart_upload(
                **parameters,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            upload_id = ""
        actual = "sha256:" + digest.hexdigest()
        if size != expected_size or actual != expected_sha256:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_SOURCE_CHANGED",
                "分析输出在 Artifact Export 期间发生变化。",
            )
        response = _s3_head(client, profile, key)
        if response is None:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_DESTINATION_UNAVAILABLE",
                "Artifact Export 目标对象写入后不可见。",
                retryable=True,
                http_status=503,
            )
        return _verify_s3_existing(
            profile,
            key,
            response,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
    except (ArtifactExportLeaseLost, _ArtifactExportDeadline):
        if upload_id:
            try:
                client.abort_multipart_upload(
                    **_s3_parameters(profile, key),
                    UploadId=upload_id,
                )
            except Exception:
                pass
        raise
    except Exception as error:
        if upload_id:
            try:
                client.abort_multipart_upload(
                    **_s3_parameters(profile, key),
                    UploadId=upload_id,
                )
            except BaseException:
                pass
        if isinstance(error, ArtifactExportError):
            raise
        raise _mapped_s3_error(error) from error


def _s3_part_size(expected_size: int) -> int:
    if expected_size < 0 or expected_size > S3_MAXIMUM_OBJECT_BYTES:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_OUTPUT_TOO_LARGE",
            "分析输出超过 S3 Artifact Export 单对象上限。",
            http_status=413,
        )
    required = (expected_size + S3_MAXIMUM_PARTS - 1) // S3_MAXIMUM_PARTS
    part_size = max(
        S3_MINIMUM_MULTIPART_BYTES,
        int(settings.ANALYSIS_ARTIFACT_EXPORT_CHUNK_BYTES),
        required,
    )
    if part_size > S3_MAXIMUM_MULTIPART_BYTES:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_OUTPUT_TOO_LARGE",
            "分析输出无法在 S3 multipart 安全边界内交付。",
            http_status=413,
        )
    return part_size


def _destination_key(
    profile: ArtifactExportProfile,
    export: ArtifactExport,
    name: str,
) -> str:
    components = [
        *(PurePosixPath(profile.prefix).parts if profile.prefix else ()),
        str(export.run_id),
        str(export.id),
        name,
    ]
    return "/".join(components)


def _manifest_item(item: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "key",
        "name",
        "label",
        "semantic_type",
        "wdl_type",
        "required",
        "kind",
        "filename",
        "size",
        "content_type",
        "sha256",
    )
    result = {field: item[field] for field in fields if field in item}
    if item.get("kind") == "value":
        result["value"] = item.get("value")
    return result


def _freeze_manifest_completed_at(export: ArtifactExport):
    if export.manifest_completed_at is not None:
        _renew_export_lease(export, force=True)
        return export.manifest_completed_at
    _renew_export_lease(export, force=True)
    now = timezone.now()
    updated = ArtifactExport.objects.filter(
        pk=export.pk,
        state=ArtifactExport.State.EXPORTING,
        lease_token=export.lease_token,
        lease_expires_at__gte=now,
        manifest_completed_at__isnull=True,
    ).update(manifest_completed_at=now, updated_at=now)
    if updated != 1:
        raise ArtifactExportLeaseLost()
    export.manifest_completed_at = now
    return now


def _delivery_manifest(
    export: ArtifactExport,
    *,
    completed_at,
    items: list[dict[str, Any]],
    files_exported: int,
    bytes_exported: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "export_id": str(export.id),
        "run_id": str(export.run_id),
        "source_manifest_digest": export.source_manifest_digest,
        "target": {
            key: value
            for key, value in export.target_snapshot.items()
            if key != "routing_digest"
        },
        "completed_at": completed_at.isoformat(),
        "items": items,
        "summary": {
            "file_count": files_exported,
            "item_count": len(items),
            "total_bytes": bytes_exported,
        },
    }


def _freeze_delivery_manifest(
    export: ArtifactExport,
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], str, bytes]:
    encoded = _canonical_bytes(candidate)
    if len(encoded) > int(settings.ANALYSIS_ARTIFACT_EXPORT_MANIFEST_MAX_BYTES):
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_MANIFEST_TOO_LARGE",
            "Artifact Export 交付清单超过大小上限。",
        )
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    frozen = export.manifest if isinstance(export.manifest, dict) else {}
    if frozen or export.manifest_digest:
        frozen_encoded = _canonical_bytes(frozen)
        frozen_digest = "sha256:" + hashlib.sha256(frozen_encoded).hexdigest()
        if (
            frozen != candidate
            or not export.manifest_digest
            or frozen_digest != export.manifest_digest
        ):
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_MANIFEST_CHANGED",
                "Artifact Export 已固化的交付清单与本次目标证据不一致。",
            )
        _renew_export_lease(export, force=True)
        return frozen, frozen_digest, frozen_encoded
    _renew_export_lease(export, force=True)
    now = timezone.now()
    updated = ArtifactExport.objects.filter(
        pk=export.pk,
        state=ArtifactExport.State.EXPORTING,
        lease_token=export.lease_token,
        lease_expires_at__gte=now,
        manifest_digest="",
    ).update(
        manifest=candidate,
        manifest_digest=digest,
        updated_at=now,
    )
    if updated != 1:
        raise ArtifactExportLeaseLost()
    export.manifest = candidate
    export.manifest_digest = digest
    return candidate, digest, encoded


def _perform_artifact_export(
    export: ArtifactExport,
) -> tuple[dict[str, Any], str, dict[str, Any], int, int]:
    export.run.refresh_from_db()
    export.service_account.refresh_from_db(fields=["is_active"])
    if not export.service_account.is_active:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_SERVICE_ACCOUNT_INACTIVE",
            "Service Account 已停用。",
        )
    if export.run.status != AnalysisRun.Status.SUCCEEDED:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_RUN_CHANGED",
            "分析运行不再满足导出条件。",
        )
    if _digest(export.run.output_manifest) != export.source_manifest_digest:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_SOURCE_MANIFEST_CHANGED",
            "分析输出清单在导出请求后发生变化。",
        )
    items = _source_items(export.run)
    export._artifact_files_total = sum(item.get("kind") == "file" for item in items)
    export._artifact_files_exported = 0
    export._artifact_bytes_exported = 0
    profile = load_artifact_export_profile(
        export.target_profile,
        client_id=export.service_account.client_id,
    )
    _validate_profile_snapshot(export, profile)
    exported_items: list[dict[str, Any]] = []
    files_exported = 0
    bytes_exported = 0

    def append_value(item: dict[str, Any]) -> None:
        exported_items.append(_manifest_item(item))

    def export_files_managed(directory: int, base_relative: str) -> None:
        nonlocal files_exported, bytes_exported
        for index, item in enumerate(items, start=1):
            if item.get("kind") == "value":
                append_value(item)
                continue
            name = _item_destination_name(index, item)
            handle = None
            try:
                _, handle = open_verified_output(
                    item,
                    run_root=export.run.work_directory,
                )
                location = _write_managed_stream(
                    directory,
                    base_relative,
                    profile,
                    export,
                    name=name,
                    source=handle,
                    expected_size=int(item["size"]),
                    expected_sha256=str(item["sha256"]),
                )
            except (KeyError, OSError, TypeError, ValueError) as error:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_SOURCE_CHANGED",
                    "分析输出与成功时的不可变清单不一致。",
                ) from error
            finally:
                if handle is not None:
                    handle.close()
            exported = _manifest_item(item)
            exported["destination"] = location
            exported_items.append(exported)
            files_exported += 1
            bytes_exported += int(item["size"])
            export._artifact_files_exported = files_exported
            export._artifact_bytes_exported = bytes_exported

    def export_files_s3(client) -> None:
        nonlocal files_exported, bytes_exported
        for index, item in enumerate(items, start=1):
            if item.get("kind") == "value":
                append_value(item)
                continue
            name = _item_destination_name(index, item)
            key = _destination_key(profile, export, name)
            handle = None
            try:
                _, handle = open_verified_output(
                    item,
                    run_root=export.run.work_directory,
                )
                location = _write_s3_stream(
                    client,
                    profile,
                    export,
                    key=key,
                    source=handle,
                    expected_size=int(item["size"]),
                    expected_sha256=str(item["sha256"]),
                    content_type=str(item.get("content_type") or "application/octet-stream"),
                )
            except (KeyError, OSError, TypeError, ValueError) as error:
                raise ArtifactExportError(
                    "ARTIFACT_EXPORT_SOURCE_CHANGED",
                    "分析输出与成功时的不可变清单不一致。",
                ) from error
            finally:
                if handle is not None:
                    handle.close()
            exported = _manifest_item(item)
            exported["destination"] = location
            exported_items.append(exported)
            files_exported += 1
            bytes_exported += int(item["size"])
            export._artifact_files_exported = files_exported
            export._artifact_bytes_exported = bytes_exported

    if profile.kind == "managed_directory":
        with _managed_export_directory(profile, export) as (directory, base_relative):
            export_files_managed(directory, base_relative)
            completed_at = _freeze_manifest_completed_at(export)
            manifest, manifest_digest, encoded = _freeze_delivery_manifest(
                export,
                _delivery_manifest(
                    export,
                    completed_at=completed_at,
                    items=exported_items,
                    files_exported=files_exported,
                    bytes_exported=bytes_exported,
                ),
            )
            manifest_location = _write_managed_stream(
                directory,
                base_relative,
                profile,
                export,
                name="manifest.json",
                source=io.BytesIO(encoded),
                expected_size=len(encoded),
                expected_sha256=manifest_digest,
            )
    else:
        client = _s3_client(profile)
        try:
            export_files_s3(client)
            completed_at = _freeze_manifest_completed_at(export)
            manifest, manifest_digest, encoded = _freeze_delivery_manifest(
                export,
                _delivery_manifest(
                    export,
                    completed_at=completed_at,
                    items=exported_items,
                    files_exported=files_exported,
                    bytes_exported=bytes_exported,
                ),
            )
            manifest_location = _write_s3_stream(
                client,
                profile,
                export,
                key=_destination_key(profile, export, "manifest.json"),
                source=io.BytesIO(encoded),
                expected_size=len(encoded),
                expected_sha256=manifest_digest,
                content_type="application/json",
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    return manifest, manifest_digest, manifest_location, files_exported, bytes_exported


def _finish_artifact_export(
    export: ArtifactExport,
    *,
    result: tuple[dict[str, Any], str, dict[str, Any], int, int] | None,
    error: ArtifactExportError | None,
) -> bool:
    with transaction.atomic():
        current = (
            ArtifactExport.objects.select_for_update(of=("self", "service_account"))
            .select_related("run", "service_account", "retention")
            .filter(
                pk=export.pk,
                state=ArtifactExport.State.EXPORTING,
                lease_token=export.lease_token,
            )
            .first()
        )
        if current is None:
            return False
        attempt = ArtifactExportAttempt.objects.select_for_update().get(
            export=current,
            attempt_number=current.attempt_count,
        )
        now = timezone.now()
        if result is not None and not current.service_account.is_active:
            result = None
            error = ArtifactExportError(
                "ARTIFACT_EXPORT_SERVICE_ACCOUNT_INACTIVE",
                "Service Account 已停用。",
            )
        if result is not None:
            manifest, manifest_digest, manifest_location, files_exported, bytes_exported = result
            current.state = ArtifactExport.State.SUCCEEDED
            current.manifest = manifest
            current.manifest_digest = manifest_digest
            current.manifest_location = manifest_location
            current.completed_at = current.manifest_completed_at or now
            current.last_error_code = ""
            current.last_error = ""
            current.last_error_retryable = False
            attempt.outcome = ArtifactExportAttempt.Outcome.SUCCEEDED
            attempt.files_total = int(manifest["summary"]["file_count"])
            attempt.files_exported = files_exported
            attempt.bytes_exported = bytes_exported
        else:
            caught = error or ArtifactExportError(
                "ARTIFACT_EXPORT_FAILED",
                "Artifact Export 执行失败。",
                retryable=True,
            )
            maximum = max(1, int(settings.ANALYSIS_ARTIFACT_EXPORT_MAX_ATTEMPTS))
            retry = caught.retryable and current.attempt_count < maximum
            current.state = (
                ArtifactExport.State.PENDING
                if retry
                else ArtifactExport.State.DEAD_LETTER
            )
            if retry:
                current.next_attempt_at = now + _retry_delay(current.attempt_count)
                attempt.outcome = ArtifactExportAttempt.Outcome.RETRY
            else:
                attempt.outcome = ArtifactExportAttempt.Outcome.DEAD_LETTER
            current.last_error_code = caught.code
            current.last_error = str(caught)[:MAX_ERROR_CHARS]
            current.last_error_retryable = caught.retryable
            attempt.error_code = caught.code
            attempt.error = str(caught)[:MAX_ERROR_CHARS]
            attempt.files_total = int(
                getattr(export, "_artifact_files_total", attempt.files_total)
            )
            attempt.files_exported = int(
                getattr(export, "_artifact_files_exported", 0)
            )
            attempt.bytes_exported = int(
                getattr(export, "_artifact_bytes_exported", 0)
            )
        current.lease_token = None
        current.lease_expires_at = None
        current.save()
        attempt.finished_at = now
        attempt.save()
        AnalysisRunEvent.objects.create(
            run=current.run,
            kind="artifact_export",
            level="info" if result is not None else "error",
            message=(
                "Artifact Export 已完成。"
                if result is not None
                else "Artifact Export 本次尝试失败。"
            ),
            details={
                "artifact_export_id": str(current.id),
                "state": current.state,
                "attempt": current.attempt_count,
                "error_code": current.last_error_code or None,
            },
        )
        if result is not None:
            from .webhooks import enqueue_artifact_export_event

            enqueue_artifact_export_event(current)
        return True


def deliver_artifact_export(export: ArtifactExport) -> bool:
    result = None
    caught_error = None
    try:
        with _artifact_export_wall_clock_timeout(
            float(settings.ANALYSIS_ARTIFACT_EXPORT_TIMEOUT_SECONDS)
        ):
            result = _perform_artifact_export(export)
    except _ArtifactExportDeadline:
        caught_error = ArtifactExportError(
            "ARTIFACT_EXPORT_TIMEOUT",
            "Artifact Export 超过总时间上限。",
            retryable=True,
            http_status=503,
        )
    except ArtifactExportLeaseLost:
        return False
    except ArtifactExportError as error:
        caught_error = error
    except Exception:
        caught_error = ArtifactExportError(
            "ARTIFACT_EXPORT_FAILED",
            "Artifact Export 执行失败。",
            retryable=True,
            http_status=503,
        )
    return _finish_artifact_export(export, result=result, error=caught_error)


def replay_artifact_export(
    export_id: uuid.UUID | str,
    *,
    actor: str,
) -> ArtifactExport:
    try:
        normalized = uuid.UUID(str(export_id))
    except (TypeError, ValueError) as error:
        raise ArtifactExportError(
            "ARTIFACT_EXPORT_ID_INVALID",
            "Artifact Export ID 无效。",
        ) from error
    with transaction.atomic():
        export = (
            ArtifactExport.objects.select_for_update(of=("self",))
            .select_related("run", "service_account", "retention")
            .filter(pk=normalized)
            .first()
        )
        if export is None:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_NOT_FOUND",
                "Artifact Export 不存在。",
                http_status=404,
            )
        if export.state != ArtifactExport.State.DEAD_LETTER:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_NOT_DEAD_LETTER",
                "只有 dead_letter Artifact Export 可以人工重放。",
                http_status=409,
            )
        if not export.service_account.is_active:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_SERVICE_ACCOUNT_INACTIVE",
                "Service Account 已停用。",
                http_status=409,
            )
        if (
            export.retention.state
            in {
                AnalysisOutputRetention.State.CLEANING,
                AnalysisOutputRetention.State.CLEANED,
            }
            or export.retention.quarantined_at is not None
        ):
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_OUTPUT_CLEANING",
                "分析输出正在清理或已经清理，不能重放。",
                http_status=409,
            )
        now = timezone.now()
        export.state = ArtifactExport.State.PENDING
        export.replay_count += 1
        export.next_attempt_at = now
        export.lease_token = None
        export.lease_expires_at = None
        export.completed_at = None
        export.last_replayed_at = now
        export.last_replayed_by = str(actor or "deployment")[:256]
        export.save()
        AnalysisRunEvent.objects.create(
            run=export.run,
            kind="artifact_export",
            message="已人工重放 dead-letter Artifact Export。",
            details={
                "artifact_export_id": str(export.id),
                "replay_count": export.replay_count,
                "actor": export.last_replayed_by,
            },
        )
        return export


def artifact_export_metrics() -> dict[str, Any]:
    counts = {
        item["state"]: item["count"]
        for item in ArtifactExport.objects.values("state").annotate(count=Count("id"))
    }
    now = timezone.now()
    pending = ArtifactExport.objects.filter(state=ArtifactExport.State.PENDING)
    oldest = pending.aggregate(value=Min("created_at"))["value"]
    return {
        "states": {
            state: int(counts.get(state, 0)) for state, _ in ArtifactExport.State.choices
        },
        "due": pending.filter(next_attempt_at__lte=now).count(),
        "oldest_pending_seconds": (
            max(0, int((now - oldest).total_seconds())) if oldest is not None else None
        ),
    }


def output_cleanup_candidates(
    *,
    run_id: uuid.UUID | str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    queryset = AnalysisOutputRetention.objects.select_related("run").prefetch_related(
        "artifact_exports"
    )
    if run_id is not None:
        try:
            normalized = uuid.UUID(str(run_id))
        except (TypeError, ValueError) as error:
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_RUN_ID_INVALID",
                "AnalysisRun ID 无效。",
            ) from error
        queryset = queryset.filter(run_id=normalized)
    results = []
    now = timezone.now()
    for retention in queryset.order_by("retain_until", "created_at")[: max(1, limit)]:
        blockers = _retention_blockers(retention, now=now)
        results.append(
            {
                "run_id": str(retention.run_id),
                "state": retention.state,
                "retain_until": retention.retain_until.isoformat(),
                "eligible": not blockers,
                "blockers": blockers,
                "cleanup_attempt_count": retention.cleanup_attempt_count,
                "quarantined_at": (
                    retention.quarantined_at.isoformat()
                    if retention.quarantined_at
                    else None
                ),
            }
        )
    return results


def _claim_expired_output_cleanup(
    now,
    *,
    run_id: uuid.UUID | None = None,
    exclude_run_ids: set[uuid.UUID] | None = None,
) -> AnalysisOutputRetention | None:
    queryset = (
        AnalysisOutputRetention.objects.select_for_update(
            skip_locked=True,
            of=("self",),
        )
        .select_related("run")
        .filter(
            state=AnalysisOutputRetention.State.CLEANING,
            cleanup_expires_at__lt=now,
            cleanup_token__isnull=False,
            cleanup_path_token__isnull=False,
        )
        .order_by("cleanup_expires_at")
    )
    if run_id is not None:
        queryset = queryset.filter(run_id=run_id)
    if exclude_run_ids:
        queryset = queryset.exclude(run_id__in=exclude_run_ids)
    retention = queryset.first()
    if retention is None:
        return None
    retention.cleanup_token = uuid.uuid4()
    retention.cleanup_attempt_count += 1
    retention.cleanup_expires_at = now + timedelta(
        seconds=int(settings.ANALYSIS_ARTIFACT_CLEANUP_LEASE_SECONDS)
    )
    retention.last_error_code = "ANALYSIS_OUTPUT_CLEANUP_LEASE_EXPIRED"
    retention.last_error = "上一次输出清理租约过期，正在恢复隔离目录。"
    retention.save()
    return retention


def claim_next_output_cleanup(
    *,
    run_id: uuid.UUID | str | None = None,
    exclude_run_ids: set[uuid.UUID] | None = None,
) -> AnalysisOutputRetention | None:
    normalized = None
    if run_id is not None:
        try:
            normalized = uuid.UUID(str(run_id))
        except (TypeError, ValueError) as error:
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_RUN_ID_INVALID",
                "AnalysisRun ID 无效。",
            ) from error
    with transaction.atomic():
        now = timezone.now()
        expired = _claim_expired_output_cleanup(
            now,
            run_id=normalized,
            exclude_run_ids=exclude_run_ids,
        )
        if expired is not None:
            return expired
        exports = ArtifactExport.objects.filter(retention_id=OuterRef("pk"))
        queryset = (
            AnalysisOutputRetention.objects.select_for_update(
                skip_locked=True,
                of=("self",),
            )
            .select_related("run")
            .annotate(
                has_artifact_export=Exists(exports),
                has_incomplete_export=Exists(
                    exports.exclude(state=ArtifactExport.State.SUCCEEDED)
                ),
                has_unacknowledged_export=Exists(
                    exports.filter(
                        requires_ack=True,
                        acknowledged_at__isnull=True,
                    )
                ),
            )
            .filter(
                state__in={
                    AnalysisOutputRetention.State.PROTECTED,
                    AnalysisOutputRetention.State.FAILED,
                },
                retain_until__lte=now,
                run__status__in=TERMINAL_RUN_STATES,
                has_artifact_export=True,
                has_incomplete_export=False,
                has_unacknowledged_export=False,
            )
            .filter(
                Q(quarantined_at__isnull=False)
                | Q(run__output_status=AnalysisRun.OutputStatus.COMPLETE)
            )
            .order_by("retain_until", "created_at")
        )
        if normalized is not None:
            queryset = queryset.filter(run_id=normalized)
        if exclude_run_ids:
            queryset = queryset.exclude(run_id__in=exclude_run_ids)
        retention = queryset.first()
        if retention is None or _retention_blockers(retention, now=now):
            return None
        retention.state = AnalysisOutputRetention.State.CLEANING
        retention.cleanup_attempt_count += 1
        retention.cleanup_token = uuid.uuid4()
        if retention.cleanup_path_token is None:
            retention.cleanup_path_token = uuid.uuid4()
        retention.cleanup_expires_at = now + timedelta(
            seconds=int(settings.ANALYSIS_ARTIFACT_CLEANUP_LEASE_SECONDS)
        )
        retention.last_error_code = ""
        retention.last_error = ""
        retention.save()
        return retention


def _mapped_run_directory(run: AnalysisRun) -> tuple[Path, str]:
    if not run.work_directory:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
            "AnalysisRun 缺少可清理的受管工作目录。",
        )
    local_root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    execution_root = Path(settings.ANALYSIS_RUN_EXECUTION_ROOT).resolve()
    if local_root == Path("/"):
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
            "AnalysisRun 清理根目录不能是文件系统根目录。",
        )
    raw = Path(run.work_directory)
    if not raw.is_absolute():
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
            "AnalysisRun 工作目录不是绝对受管路径。",
        )
    relative = None
    for root in (execution_root, local_root):
        try:
            relative = raw.relative_to(root)
            break
        except ValueError:
            continue
    if relative is None or relative.parts != (str(run.id),):
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
            "只允许清理 AnalysisRun 根目录下与 run ID 完全匹配的目录。",
        )
    return local_root, str(run.id)


def _renew_cleanup_lease(
    retention: AnalysisOutputRetention,
    *,
    force: bool = False,
) -> None:
    if retention.cleanup_token is None:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_LEASE_LOST",
            "输出清理租约已经失效。",
            retryable=True,
            http_status=503,
        )
    interval = min(
        30.0,
        max(1.0, float(settings.ANALYSIS_ARTIFACT_CLEANUP_LEASE_SECONDS) / 3),
    )
    monotonic_now = time.monotonic()
    last_renewed = float(getattr(retention, "_lease_renewed_monotonic", 0.0))
    if not force and monotonic_now - last_renewed < interval:
        return
    now = timezone.now()
    updated = AnalysisOutputRetention.objects.filter(
        pk=retention.pk,
        state=AnalysisOutputRetention.State.CLEANING,
        cleanup_token=retention.cleanup_token,
        cleanup_expires_at__gte=now,
    ).update(
        cleanup_expires_at=now
        + timedelta(seconds=int(settings.ANALYSIS_ARTIFACT_CLEANUP_LEASE_SECONDS)),
        updated_at=now,
    )
    if updated != 1:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_LEASE_LOST",
            "输出清理租约已经失效。",
            retryable=True,
            http_status=503,
        )
    retention._lease_renewed_monotonic = monotonic_now


def _decoded_mount_path(value: str) -> Path:
    decoded = re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )
    return Path(decoded)


def _assert_no_nested_mounts(path: Path) -> None:
    try:
        source = path.resolve(strict=True)
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8",
            errors="strict",
        ).splitlines()
    except (OSError, UnicodeError) as error:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_MOUNT_CHECK_FAILED",
            "无法验证待清理目录的 mount 边界。",
            retryable=True,
            http_status=503,
        ) from error
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_MOUNT_CHECK_FAILED",
                "系统 mount 信息格式无效。",
                retryable=True,
                http_status=503,
            )
        mountpoint = _decoded_mount_path(fields[4])
        if mountpoint == source or source in mountpoint.parents:
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_NESTED_MOUNT",
                "待清理输出目录包含独立 mount，拒绝跨挂载点删除。",
            )


def _validate_cleanup_tree_at(
    parent: int,
    name: str,
    *,
    retention: AnalysisOutputRetention,
    expected_device: int,
) -> None:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    except OSError as error:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
            "待清理输出目录无法安全打开。",
        ) from error
    try:
        if os.fstat(descriptor).st_dev != expected_device:
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_NESTED_MOUNT",
                "待清理输出目录跨越了文件系统边界。",
            )
        with os.scandir(descriptor) as iterator:
            names = sorted(entry.name for entry in iterator)
        for child in names:
            _renew_cleanup_lease(retention)
            metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if metadata.st_dev != expected_device:
                raise ArtifactExportError(
                    "ANALYSIS_OUTPUT_CLEANUP_NESTED_MOUNT",
                    "待清理输出目录跨越了文件系统边界。",
                )
            if stat_module.S_ISDIR(metadata.st_mode):
                _validate_cleanup_tree_at(
                    descriptor,
                    child,
                    retention=retention,
                    expected_device=expected_device,
                )
                continue
            if stat_module.S_ISREG(metadata.st_mode) or stat_module.S_ISLNK(
                metadata.st_mode
            ):
                continue
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
                "输出目录包含不受支持的文件系统节点，未执行删除。",
            )
    except OSError as error:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_FAILED",
            "预检待清理分析输出失败。",
            retryable=True,
            http_status=503,
        ) from error
    finally:
        os.close(descriptor)


def _mark_cleanup_quarantined(retention: AnalysisOutputRetention) -> None:
    if retention.cleanup_token is None:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_LEASE_LOST",
            "输出清理租约已经失效。",
            retryable=True,
            http_status=503,
        )
    now = timezone.now()
    updated = AnalysisOutputRetention.objects.filter(
        pk=retention.pk,
        state=AnalysisOutputRetention.State.CLEANING,
        cleanup_token=retention.cleanup_token,
        cleanup_expires_at__gte=now,
    ).update(quarantined_at=now, updated_at=now)
    if updated != 1:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_LEASE_LOST",
            "输出清理租约已经失效。",
            retryable=True,
            http_status=503,
        )
    retention.quarantined_at = now


def _delete_tree_at(
    parent: int,
    name: str,
    *,
    retention: AnalysisOutputRetention,
    expected_device: int,
) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    except OSError as error:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
            "隔离后的输出目录无法安全打开。",
        ) from error
    released = 0
    try:
        if os.fstat(descriptor).st_dev != expected_device:
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_NESTED_MOUNT",
                "隔离后的输出目录跨越了文件系统边界。",
            )
        with os.scandir(descriptor) as iterator:
            names = sorted(entry.name for entry in iterator)
        for child in names:
            _renew_cleanup_lease(retention)
            metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if metadata.st_dev != expected_device:
                raise ArtifactExportError(
                    "ANALYSIS_OUTPUT_CLEANUP_NESTED_MOUNT",
                    "隔离后的输出目录跨越了文件系统边界。",
                )
            if stat_module.S_ISDIR(metadata.st_mode):
                released += _delete_tree_at(
                    descriptor,
                    child,
                    retention=retention,
                    expected_device=expected_device,
                )
                continue
            if stat_module.S_ISREG(metadata.st_mode):
                released += metadata.st_size
                os.unlink(child, dir_fd=descriptor)
                continue
            if stat_module.S_ISLNK(metadata.st_mode):
                os.unlink(child, dir_fd=descriptor)
                continue
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
                "输出目录包含不受支持的文件系统节点，已停止清理。",
            )
        os.fsync(descriptor)
    except OSError as error:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_FAILED",
            "删除隔离后的分析输出失败。",
            retryable=True,
            http_status=503,
        ) from error
    finally:
        os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)
    except OSError as error:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_FAILED",
            "删除隔离后的分析输出目录失败。",
            retryable=True,
            http_status=503,
        ) from error
    return released


def _cleanup_run_tree(retention: AnalysisOutputRetention) -> int:
    _renew_cleanup_lease(retention, force=True)
    root, source_name = _mapped_run_directory(retention.run)
    path_token = retention.cleanup_path_token
    if path_token is None:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_STATE_INVALID",
            "输出清理缺少隔离路径令牌。",
        )
    quarantine_name = f"{source_name}-{path_token}"
    try:
        root_descriptor = os.open(root, _directory_flags())
    except OSError as error:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_ROOT_UNAVAILABLE",
            "AnalysisRun 根目录不可用。",
            retryable=True,
            http_status=503,
        ) from error
    quarantine_descriptor = -1
    try:
        quarantine_descriptor = _open_or_create_directory(
            root_descriptor,
            ".artifact-cleanup",
        )
        if os.fstat(quarantine_descriptor).st_dev != os.fstat(root_descriptor).st_dev:
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_NESTED_MOUNT",
                "输出隔离目录跨越了文件系统边界。",
            )
        _assert_no_nested_mounts(root / ".artifact-cleanup")
        source_exists = True
        moved_source = False
        try:
            source_stat = os.stat(
                source_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            source_exists = False
        if source_exists:
            if not stat_module.S_ISDIR(source_stat.st_mode):
                raise ArtifactExportError(
                    "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE",
                    "AnalysisRun 工作目录不是普通目录。",
                )
            if retention.quarantined_at is not None:
                raise ArtifactExportError(
                    "ANALYSIS_OUTPUT_CLEANUP_PATH_CONFLICT",
                    "原输出已经隔离，但同名 AnalysisRun 目录再次出现。",
                )
            _assert_no_nested_mounts(root / source_name)
            _validate_cleanup_tree_at(
                root_descriptor,
                source_name,
                retention=retention,
                expected_device=source_stat.st_dev,
            )
            try:
                os.stat(
                    quarantine_name,
                    dir_fd=quarantine_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.rename(
                    source_name,
                    quarantine_name,
                    src_dir_fd=root_descriptor,
                    dst_dir_fd=quarantine_descriptor,
                )
                os.fsync(root_descriptor)
                os.fsync(quarantine_descriptor)
                _renew_cleanup_lease(retention, force=True)
                _mark_cleanup_quarantined(retention)
                moved_source = True
            else:
                raise ArtifactExportError(
                    "ANALYSIS_OUTPUT_CLEANUP_PATH_CONFLICT",
                    "输出清理隔离路径已存在，同时源目录仍存在。",
                )
        try:
            quarantine_stat = os.stat(
                quarantine_name,
                dir_fd=quarantine_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if retention.quarantined_at is not None:
                return 0
            raise ArtifactExportError(
                "ANALYSIS_OUTPUT_CLEANUP_SOURCE_MISSING",
                "AnalysisRun 输出目录不存在，未执行删除。",
            )
        if retention.quarantined_at is None:
            _mark_cleanup_quarantined(retention)
        if not moved_source:
            _assert_no_nested_mounts(root / ".artifact-cleanup" / quarantine_name)
            _validate_cleanup_tree_at(
                quarantine_descriptor,
                quarantine_name,
                retention=retention,
                expected_device=quarantine_stat.st_dev,
            )
        return _delete_tree_at(
            quarantine_descriptor,
            quarantine_name,
            retention=retention,
            expected_device=quarantine_stat.st_dev,
        )
    except OSError as error:
        raise ArtifactExportError(
            "ANALYSIS_OUTPUT_CLEANUP_FAILED",
            "隔离 AnalysisRun 输出目录失败。",
            retryable=True,
            http_status=503,
        ) from error
    finally:
        if quarantine_descriptor >= 0:
            os.close(quarantine_descriptor)
        os.close(root_descriptor)


def _finish_output_cleanup(
    retention: AnalysisOutputRetention,
    *,
    released_bytes: int | None,
    error: ArtifactExportError | None,
    actor: str,
) -> bool:
    with transaction.atomic():
        locked_run = AnalysisRun.objects.select_for_update(of=("self",)).get(
            pk=retention.run_id
        )
        current = (
            AnalysisOutputRetention.objects.select_for_update(of=("self",))
            .filter(
                pk=retention.pk,
                state=AnalysisOutputRetention.State.CLEANING,
                cleanup_token=retention.cleanup_token,
            )
            .first()
        )
        if current is None:
            return False
        current.run = locked_run
        now = timezone.now()
        if error is None:
            current.state = AnalysisOutputRetention.State.CLEANED
            current.cleaned_at = now
            current.last_error_code = ""
            current.last_error = ""
            level = "info"
            message = "已按显式保留策略清理本地 AnalysisRun 输出。"
            details = {
                "released_bytes": int(released_bytes or 0),
                "actor": actor[:256],
            }
        else:
            current.state = AnalysisOutputRetention.State.FAILED
            current.last_error_code = error.code
            current.last_error = str(error)[:MAX_ERROR_CHARS]
            level = "error"
            message = "AnalysisRun 输出清理失败。"
            details = {
                "error_code": error.code,
                "actor": actor[:256],
            }
        current.run.output_status = (
            AnalysisRun.OutputStatus.UNAVAILABLE
            if error is None or current.quarantined_at is not None
            else AnalysisRun.OutputStatus.COMPLETE
        )
        current.run.save(update_fields=["output_status", "updated_at"])
        current.cleanup_token = None
        current.cleanup_expires_at = None
        current.save()
        AnalysisRunEvent.objects.create(
            run=current.run,
            kind="artifact_cleanup",
            level=level,
            message=message,
            details=details,
        )
        return True


def clean_analysis_output(
    retention: AnalysisOutputRetention,
    *,
    actor: str,
) -> tuple[bool, int, ArtifactExportError | None]:
    released = 0
    caught = None
    try:
        released = _cleanup_run_tree(retention)
    except ArtifactExportError as error:
        caught = error
    finalized = _finish_output_cleanup(
        retention,
        released_bytes=released if caught is None else None,
        error=caught,
        actor=actor,
    )
    return finalized, released, caught

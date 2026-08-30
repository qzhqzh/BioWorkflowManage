from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import quote

from .api import (
    ConnectorConflictError,
    ConnectorError,
    ConnectorIntegrityError,
    IntegrationClient,
    SubmissionUncertainError,
)
from .mapping import MappingConfig, canonical_digest
from .store import ConnectorStore, EventResult, OrderRecord


SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
RUN_STATUSES = {
    "queued",
    "preparing",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "canceled",
}
TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
OUTPUT_STATUSES = {"pending", "complete", "incomplete", "unavailable"}
TERMINAL_OUTPUT_STATUSES = {"complete", "incomplete", "unavailable"}
WEBHOOK_EVENT_TYPES = {
    "analysis.run.terminal",
    "analysis.artifact_export.completed",
}
MAX_WEBHOOK_BYTES = 1024 * 1024

ARTIFACT_MANIFEST_FIELDS = {
    "schema_version",
    "export_id",
    "run_id",
    "source_manifest_digest",
    "target",
    "completed_at",
    "items",
    "summary",
}
ARTIFACT_ITEM_FIELDS = {
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
    "value",
    "destination",
}
ANALYSIS_OUTPUT_FIELDS = {
    "key",
    "name",
    "label",
    "semantic_type",
    "wdl_type",
    "required",
    "kind",
    "reason",
    "filename",
    "size",
    "content_type",
    "sha256",
    "entry_count",
    "digest",
    "value",
    "download_url",
}


@dataclass(frozen=True)
class VerifiedWebhook:
    delivery_id: str
    event_id: str
    timestamp: int
    payload: dict[str, Any]
    payload_digest: str


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_datetime(value: Any) -> bool:
    if not _nonempty_string(value):
        return False
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_artifact_target(target: Any) -> None:
    if not isinstance(target, dict):
        raise ConnectorIntegrityError("Artifact Export target 无效。")
    target_type = target.get("type")
    if target_type == "managed_directory":
        required = {"type", "profile", "root_alias"}
        allowed = required | {"public_uri_prefix"}
    elif target_type == "s3":
        required = {"type", "profile", "endpoint_origin", "bucket", "prefix"}
        allowed = required
    else:
        raise ConnectorIntegrityError("Artifact Export target type 无效。")
    if not required.issubset(target) or set(target) - allowed:
        raise ConnectorIntegrityError("Artifact Export target 字段无效。")
    nonempty_fields = required - {"type", "prefix"}
    if any(not _nonempty_string(target[field]) for field in nonempty_fields):
        raise ConnectorIntegrityError("Artifact Export target 标识不能为空。")
    if "prefix" in target and not isinstance(target["prefix"], str):
        raise ConnectorIntegrityError("Artifact Export target prefix 无效。")
    if "public_uri_prefix" in target and not isinstance(
        target["public_uri_prefix"], str
    ):
        raise ConnectorIntegrityError("Artifact Export public_uri_prefix 无效。")


def _validate_artifact_export_response(
    export: Any,
    *,
    run_id: str,
    request: dict[str, Any] | None,
    expected_export_id: str | None = None,
) -> str:
    if not isinstance(export, dict) or not isinstance(request, dict):
        raise ConnectorIntegrityError("Artifact Export 请求或响应身份无效。")
    export_id = str(export.get("id") or "")
    target = export.get("target")
    requested_target = request.get("target")
    if (
        not export_id
        or (expected_export_id is not None and export_id != expected_export_id)
        or export.get("run_id") != run_id
        or not isinstance(requested_target, dict)
        or not isinstance(target, dict)
        or target.get("profile") != requested_target.get("profile")
        or export.get("requires_ack") is not request.get("requires_ack")
    ):
        raise ConnectorIntegrityError("Artifact Export 响应身份或目标不一致。")
    _validate_artifact_target(target)
    return export_id


def _validate_artifact_destination(
    destination: Any,
    *,
    expected_size: int,
    expected_digest: str,
    target: dict[str, Any],
    run_id: str,
    export_id: str,
) -> None:
    if not isinstance(destination, dict):
        raise ConnectorIntegrityError("Artifact Export 文件缺少 destination。")
    destination_type = destination.get("type")
    if destination_type == "managed_directory":
        required = {
            "type",
            "profile",
            "root_alias",
            "relative_path",
            "uri",
            "size",
            "sha256",
        }
        allowed = required
    elif destination_type == "s3_object":
        required = {"type", "profile", "bucket", "key", "uri", "size", "sha256"}
        allowed = required | {"etag", "version_id"}
    else:
        raise ConnectorIntegrityError("Artifact Export destination type 无效。")
    if not required.issubset(destination) or set(destination) - allowed:
        raise ConnectorIntegrityError("Artifact Export destination 字段无效。")
    text_fields = required - {"type", "size", "sha256"}
    if any(not _nonempty_string(destination[field]) for field in text_fields):
        raise ConnectorIntegrityError("Artifact Export destination 标识不能为空。")
    if destination.get("size") != expected_size or destination.get(
        "sha256"
    ) != expected_digest:
        raise ConnectorIntegrityError("Artifact Export destination 文件证据不一致。")
    for optional in ("etag", "version_id"):
        if optional in destination and not _nonempty_string(destination[optional]):
            raise ConnectorIntegrityError("Artifact Export destination 版本标识无效。")
    if target.get("type") == "managed_directory":
        relative_path = PurePosixPath(str(destination.get("relative_path") or ""))
        parts = relative_path.parts
        if (
            destination_type != "managed_directory"
            or destination.get("profile") != target.get("profile")
            or destination.get("root_alias") != target.get("root_alias")
            or relative_path.is_absolute()
            or ".." in parts
            or len(parts) < 3
            or parts[-3:-1] != (run_id, export_id)
        ):
            raise ConnectorIntegrityError("Artifact Export destination 与受管目标不一致。")
        public_prefix = str(target.get("public_uri_prefix") or "").rstrip("/")
        expected_uri = (
            f"{public_prefix}/{relative_path}"
            if public_prefix
            else f"managed://{target['root_alias']}/{relative_path}"
        )
    elif target.get("type") == "s3":
        key = PurePosixPath(str(destination.get("key") or ""))
        prefix = PurePosixPath(str(target.get("prefix") or ""))
        key_parts = key.parts
        prefix_parts = prefix.parts if str(prefix) != "." else ()
        namespace = (*prefix_parts, run_id, export_id)
        if (
            destination_type != "s3_object"
            or destination.get("profile") != target.get("profile")
            or destination.get("bucket") != target.get("bucket")
            or key.is_absolute()
            or ".." in key_parts
            or len(key_parts) != len(namespace) + 1
            or key_parts[: len(namespace)] != namespace
        ):
            raise ConnectorIntegrityError("Artifact Export destination 与 S3 目标不一致。")
        expected_uri = f"s3://{target['bucket']}/{key}"
    else:
        raise ConnectorIntegrityError("Artifact Export target type 无效。")
    if destination.get("uri") != expected_uri:
        raise ConnectorIntegrityError("Artifact Export destination URI 与路由不一致。")


def _validate_artifact_manifest(
    manifest: dict[str, Any],
    *,
    export_id: str,
    run_id: str,
    source_manifest_digest: Any,
    target: Any,
) -> None:
    if set(manifest) != ARTIFACT_MANIFEST_FIELDS:
        raise ConnectorIntegrityError("Artifact Export manifest 字段无效。")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("export_id") != export_id
        or manifest.get("run_id") != run_id
        or not SHA256.fullmatch(str(source_manifest_digest or ""))
        or manifest.get("source_manifest_digest") != source_manifest_digest
        or manifest.get("target") != target
        or not _valid_datetime(manifest.get("completed_at"))
    ):
        raise ConnectorIntegrityError("Artifact Export manifest 身份或来源无效。")
    _validate_artifact_target(target)
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise ConnectorIntegrityError("Artifact Export manifest 缺少 items。")
    item_keys: set[str] = set()
    file_count = 0
    total_bytes = 0
    core_fields = {"key", "name", "semantic_type", "wdl_type", "required", "kind"}
    for item in items:
        if (
            not isinstance(item, dict)
            or not core_fields.issubset(item)
            or set(item) - ARTIFACT_ITEM_FIELDS
            or any(
                not _nonempty_string(item[field])
                for field in ("key", "name", "semantic_type", "wdl_type")
            )
            or not isinstance(item.get("required"), bool)
            or item.get("kind") not in {"file", "value"}
            or str(item["key"]) in item_keys
            or ("label" in item and not isinstance(item["label"], str))
        ):
            raise ConnectorIntegrityError("Artifact Export manifest 条目无效。")
        item_keys.add(str(item["key"]))
        if item["kind"] == "value":
            if "value" not in item or any(
                field in item
                for field in (
                    "filename",
                    "size",
                    "content_type",
                    "sha256",
                    "destination",
                )
            ):
                raise ConnectorIntegrityError("Artifact Export value 条目无效。")
            continue
        file_fields = {"filename", "size", "content_type", "sha256", "destination"}
        if (
            not file_fields.issubset(item)
            or "value" in item
            or not _nonempty_string(item["filename"])
            or not _nonempty_string(item["content_type"])
            or not _nonnegative_integer(item["size"])
            or not SHA256.fullmatch(str(item["sha256"] or ""))
        ):
            raise ConnectorIntegrityError("Artifact Export 文件证据无效。")
        _validate_artifact_destination(
            item["destination"],
            expected_size=item["size"],
            expected_digest=item["sha256"],
            target=target,
            run_id=run_id,
            export_id=export_id,
        )
        file_count += 1
        total_bytes += item["size"]
    summary = manifest.get("summary")
    if not isinstance(summary, dict) or summary != {
        "file_count": file_count,
        "item_count": len(items),
        "total_bytes": total_bytes,
    }:
        raise ConnectorIntegrityError("Artifact Export manifest summary 无效。")


def verify_webhook(
    secret_token: str,
    headers: dict[str, str],
    body: bytes,
    *,
    now_timestamp: int | None = None,
    tolerance_seconds: int = 300,
) -> VerifiedWebhook:
    if len(body) > MAX_WEBHOOK_BYTES:
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_TOO_LARGE",
            "Webhook body 超过 Connector 安全上限。",
        )
    normalized = {str(key).casefold(): str(value) for key, value in headers.items()}
    try:
        delivery_id = str(uuid.UUID(normalized["x-bioworkflow-delivery-id"]))
        event_id = str(uuid.UUID(normalized["x-bioworkflow-event-id"]))
        timestamp = int(normalized["x-bioworkflow-timestamp"])
        secret_version = int(normalized["x-bioworkflow-secret-version"])
        signature = normalized["x-bioworkflow-signature"]
        padding = "=" * (-len(secret_token) % 4)
        secret = base64.b64decode(
            secret_token + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, KeyError, TypeError, ValueError) as error:
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_SIGNATURE_INVALID",
            "Webhook 签名 header 或密钥格式无效。",
        ) from error
    if len(secret) != hashlib.sha256().digest_size or secret_version < 1:
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_SIGNATURE_INVALID",
            "Webhook 密钥或 secret version 无效。",
        )
    signed = b".".join(
        [
            delivery_id.encode("ascii"),
            event_id.encode("ascii"),
            str(timestamp).encode("ascii"),
            body,
        ]
    )
    expected = "v1=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_SIGNATURE_INVALID",
            "Webhook HMAC-SHA256 签名不匹配。",
        )
    current = int(time.time()) if now_timestamp is None else int(now_timestamp)
    if abs(current - timestamp) > max(0, int(tolerance_seconds)):
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_TIMESTAMP_OUT_OF_RANGE",
            "Webhook timestamp 超出允许窗口。",
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_PAYLOAD_INVALID",
            "Webhook body 不是有效 UTF-8 JSON。",
        ) from error
    if not isinstance(payload, dict):
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_PAYLOAD_INVALID",
            "Webhook JSON 顶层必须是 object。",
        )
    if payload.get("schema_version") != "1.0.0":
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_SCHEMA_UNSUPPORTED",
            "Webhook schema_version 不受支持。",
        )
    try:
        body_event_id = str(uuid.UUID(str(payload.get("event_id") or "")))
    except ValueError as error:
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_PAYLOAD_INVALID",
            "Webhook event_id 无效。",
        ) from error
    if body_event_id != event_id or payload.get("event_type") not in WEBHOOK_EVENT_TYPES:
        raise ConnectorError(
            "CONNECTOR_WEBHOOK_PAYLOAD_INVALID",
            "Webhook header/body event_id 或 event_type 不一致。",
        )
    return VerifiedWebhook(
        delivery_id=delivery_id,
        event_id=event_id,
        timestamp=timestamp,
        payload=payload,
        payload_digest=canonical_digest(payload),
    )


class ReferenceConnector:
    def __init__(
        self,
        *,
        mapping: MappingConfig,
        client: IntegrationClient,
        store: ConnectorStore,
        webhook_secret: str,
        result_directory: str | Path,
        webhook_tolerance_seconds: int = 300,
    ) -> None:
        self.mapping = mapping
        self.client = client
        self.store = store
        self.webhook_secret = str(webhook_secret or "").strip()
        self.result_directory = Path(result_directory).expanduser().absolute()
        self.webhook_tolerance_seconds = max(0, int(webhook_tolerance_seconds))
        if not self.webhook_secret:
            raise ValueError("BIOWORKFLOW_WEBHOOK_SECRET 不能为空。")
        self._prepare_result_root()

    def _prepare_result_root(self) -> None:
        self.result_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self.result_directory.lstat()
        if not stat.S_ISDIR(current.st_mode) or self.result_directory.is_symlink():
            raise ConnectorError(
                "CONNECTOR_RESULT_PATH_INVALID",
                "Connector 结果根目录必须是真实目录。",
            )
        os.chmod(self.result_directory, 0o700)

    def discover_product(self) -> dict[str, Any]:
        if not self.mapping.expected_contract_digest:
            raise ConnectorIntegrityError(
                "提交前必须固定 analysis_product.expected_contract_digest。"
            )
        product = self.client.get_product(
            self.mapping.analysis_code,
            self.mapping.contract_version,
        )
        if product.get("analysis_code") != self.mapping.analysis_code or product.get(
            "contract_version"
        ) != self.mapping.contract_version:
            raise ConnectorIntegrityError("产品详情与 Connector 固定契约不一致。")
        if not product.get("ready"):
            raise ConnectorError(
                "CONNECTOR_ANALYSIS_PRODUCT_NOT_READY",
                "固定分析产品当前不可投递。",
                details={"blockers": product.get("blockers") or []},
            )
        if product.get("contract_digest") != self.mapping.expected_contract_digest:
            raise ConnectorIntegrityError("分析产品 contract_digest 与 Connector 固定值不一致。")
        return product

    def _run_identity(
        self,
        payload: dict[str, Any],
        external_run_id: str,
        expected_request_digest: str,
    ) -> tuple[str, str, int]:
        run_id = str(payload.get("id") or "").strip()
        external = payload.get("external_ref")
        product = payload.get("analysis_product")
        status = str(payload.get("status") or "").strip()
        output_status = str(payload.get("output_status") or "").strip()
        status_version = payload.get("status_version")
        if (
            not run_id
            or payload.get("request_digest") != expected_request_digest
            or not isinstance(external, dict)
            or external.get("client_id") != self.mapping.client_id
            or external.get("external_run_id") != external_run_id
            or not isinstance(product, dict)
            or product.get("analysis_code") != self.mapping.analysis_code
            or product.get("contract_version") != self.mapping.contract_version
            or (
                self.mapping.expected_contract_digest
                and product.get("contract_digest")
                != self.mapping.expected_contract_digest
            )
            or status not in RUN_STATUSES
            or output_status not in OUTPUT_STATUSES
            or not isinstance(status_version, int)
            or isinstance(status_version, bool)
            or status_version < 1
        ):
            raise ConnectorIntegrityError("AnalysisRun 响应缺少稳定标识或状态版本。")
        return run_id, status, status_version

    def submit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        mapped = self.mapping.map_order(order)
        self.client.require_compatible_api()
        record, created = self.store.register_order(
            mapped.external_run_id,
            mapped.request_digest,
        )
        if record.run_id:
            run = self.client.get_run(record.run_id)
            run_id, status, version = self._run_identity(
                run,
                mapped.external_run_id,
                mapped.request_digest,
            )
            self.store.update_from_poll(
                mapped.external_run_id,
                run_id=run_id,
                status=status,
                status_version=version,
                output_status=str(run.get("output_status") or "") or None,
            )
            return run
        if not created:
            recovered = self.client.find_run(mapped.external_run_id)
            if recovered is not None:
                run_id, status, status_version = self._run_identity(
                    recovered,
                    mapped.external_run_id,
                    mapped.request_digest,
                )
                self.store.bind_run(
                    mapped.external_run_id,
                    run_id,
                    status=status,
                    status_version=status_version,
                    output_status=str(recovered["output_status"]),
                )
                return recovered

        self.discover_product()
        preflight = self.client.preflight(mapped.preflight)
        if preflight.get("submission_allowed") is not True:
            raise ConnectorError(
                "CONNECTOR_PREFLIGHT_BLOCKED",
                "Analysis Request 预检不允许提交。",
                details={"checks": preflight.get("checks") or []},
            )
        run = self.client.submit_with_recovery(
            mapped.submission,
            idempotency_key=mapped.idempotency_key,
            external_run_id=mapped.external_run_id,
        )
        run_id, status, status_version = self._run_identity(
            run,
            mapped.external_run_id,
            mapped.request_digest,
        )
        self.store.bind_run(
            mapped.external_run_id,
            run_id,
            status=status,
            status_version=status_version,
            output_status=str(run["output_status"]),
        )
        return run

    def reconcile(self, external_run_id: str) -> dict[str, Any]:
        record = self.store.get_order(external_run_id)
        if record.run_id:
            run = self.client.get_run(record.run_id)
        else:
            run = self.client.find_run(external_run_id)
            if run is None:
                raise SubmissionUncertainError(external_run_id)
        run_id, status, version = self._run_identity(
            run,
            external_run_id,
            record.request_digest,
        )
        self.store.update_from_poll(
            external_run_id,
            run_id=run_id,
            status=status,
            status_version=version,
            output_status=str(run.get("output_status") or "") or None,
        )
        return run

    def handle_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        *,
        now_timestamp: int | None = None,
    ) -> EventResult:
        verified = verify_webhook(
            self.webhook_secret,
            headers,
            body,
            now_timestamp=now_timestamp,
            tolerance_seconds=self.webhook_tolerance_seconds,
        )
        payload = verified.payload
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ConnectorError(
                "CONNECTOR_WEBHOOK_PAYLOAD_INVALID",
                "Webhook data 必须是 object。",
            )
        external = data.get("external_ref")
        if not isinstance(external, dict) or external.get("client_id") != self.mapping.client_id:
            raise ConnectorError(
                "CONNECTOR_WEBHOOK_CLIENT_MISMATCH",
                "Webhook external_ref.client_id 不属于本 Connector。",
            )
        external_run_id = str(external.get("external_run_id") or "")
        run_id = str(data.get("run_id") or "")
        status_version = data.get("status_version")
        if (
            not external_run_id
            or not run_id
            or not isinstance(status_version, int)
            or isinstance(status_version, bool)
            or status_version < 1
        ):
            raise ConnectorError(
                "CONNECTOR_WEBHOOK_PAYLOAD_INVALID",
                "Webhook 缺少 run/external/status_version。",
            )
        record = self.store.get_order(external_run_id)
        if record.run_id is None:
            run = self.client.get_run(run_id)
            validated_run_id, _, _ = self._run_identity(
                run,
                external_run_id,
                record.request_digest,
            )
            if validated_run_id != run_id:
                raise ConnectorConflictError(
                    "Webhook run_id 与上游运行详情不一致。"
                )
        elif record.run_id != run_id:
            raise ConnectorConflictError("Webhook run_id 与 Connector 绑定不一致。")
        event_type = payload["event_type"]
        if event_type == "analysis.run.terminal":
            status = str(data.get("status") or "")
            output_status = str(data.get("output_status") or "")
            if (
                status not in TERMINAL_STATUSES
                or output_status not in TERMINAL_OUTPUT_STATUSES
            ):
                raise ConnectorError(
                    "CONNECTOR_WEBHOOK_PAYLOAD_INVALID",
                    "终态 Webhook 的 status 或 output_status 无效。",
                )
            return self.store.apply_terminal_event(
                event_id=verified.event_id,
                delivery_id=verified.delivery_id,
                external_run_id=external_run_id,
                run_id=run_id,
                status=status,
                status_version=status_version,
                output_status=output_status,
                payload_digest=verified.payload_digest,
            )
        export_id = str(data.get("artifact_export_id") or "")
        manifest_digest = str(data.get("manifest_digest") or "")
        if not export_id or not SHA256.fullmatch(manifest_digest):
            raise ConnectorError(
                "CONNECTOR_WEBHOOK_PAYLOAD_INVALID",
                "Artifact Webhook 缺少 export id 或清单摘要。",
            )
        replayed = self.store.find_event_replay(
            event_id=verified.event_id,
            payload_digest=verified.payload_digest,
            external_run_id=external_run_id,
        )
        if replayed is not None:
            return replayed
        if record.export_request_digest is None or record.export_request is None:
            raise ConnectorConflictError(
                "尚未固定 Artifact Export 请求，拒绝完成事件。"
            )
        export = self.client.get_artifact_export(export_id)
        _validate_artifact_export_response(
            export,
            run_id=run_id,
            request=record.export_request,
            expected_export_id=export_id,
        )
        authoritative_digest = str(export.get("manifest_digest") or "")
        manifest = export.get("manifest")
        if (
            export.get("state") != "succeeded"
            or not SHA256.fullmatch(authoritative_digest)
            or authoritative_digest != manifest_digest
            or not isinstance(manifest, dict)
            or canonical_digest(manifest) != authoritative_digest
        ):
            raise ConnectorIntegrityError(
                "Artifact 完成 Webhook 与权威 export 状态或清单摘要不一致。"
            )
        _validate_artifact_manifest(
            manifest,
            export_id=export_id,
            run_id=run_id,
            source_manifest_digest=export.get("source_manifest_digest"),
            target=export.get("target"),
        )
        return self.store.apply_export_event(
            event_id=verified.event_id,
            delivery_id=verified.delivery_id,
            external_run_id=external_run_id,
            run_id=run_id,
            status_version=status_version,
            export_id=export_id,
            manifest_digest=manifest_digest,
            payload_digest=verified.payload_digest,
        )

    @staticmethod
    def _safe_component(value: str, *, fallback: str) -> str:
        normalized = SAFE_COMPONENT.sub("-", value).strip(".-")[:96]
        return normalized or fallback

    def _order_result_directory(self, external_run_id: str) -> Path:
        component = self._safe_component(external_run_id, fallback="order")
        digest = hashlib.sha256(external_run_id.encode("utf-8")).hexdigest()[:12]
        directory = self.result_directory / f"{component}-{digest}"
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            current = directory.lstat()
            if not stat.S_ISDIR(current.st_mode) or directory.is_symlink():
                raise ConnectorIntegrityError("MES 结果目录被非目录对象占用。")
        return directory

    def _install_output(
        self,
        external_run_id: str,
        *,
        key: str,
        digest: str,
        content: bytes,
    ) -> Path:
        directory = self._order_result_directory(external_run_id)
        component = self._safe_component(key, fallback="output")
        target = directory / f"{component}-{digest[7:19]}"
        if target.exists() or target.is_symlink():
            current = target.lstat()
            if not stat.S_ISREG(current.st_mode) or target.is_symlink():
                raise ConnectorIntegrityError("已有结果路径不是普通文件。")
            existing = target.read_bytes()
            existing_digest = "sha256:" + hashlib.sha256(existing).hexdigest()
            if existing_digest != digest or existing != content:
                raise ConnectorConflictError("已有结果文件与本次摘要不一致。")
            return target
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=directory,
                prefix=".connector-output-",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
            temporary_name = ""
            return target
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _output_download_url(external_run_id: str, output_key: str) -> str:
        return (
            f"/v1/orders/{quote(external_run_id, safe='')}/outputs/"
            f"{quote(output_key, safe='')}"
        )

    def open_output(
        self,
        external_run_id: str,
        output_key: str,
    ) -> tuple[dict[str, Any], BinaryIO]:
        receipt = self.store.get_output(external_run_id, output_key)
        path = Path(str(receipt["local_path"]))
        if (
            not path.is_absolute()
            or path.parent.parent != self.result_directory
            or path.parent.is_symlink()
        ):
            raise ConnectorIntegrityError("Connector 输出落盘路径越出结果根目录。")
        try:
            parent = path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode):
                raise ConnectorIntegrityError("Connector 输出目录已被替换。")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            handle = os.fdopen(descriptor, "rb")
        except OSError as error:
            raise ConnectorIntegrityError("Connector 已验证输出当前不可读取。") from error
        try:
            current = os.fstat(handle.fileno())
            if not stat.S_ISREG(current.st_mode):
                raise ConnectorIntegrityError("Connector 输出不再是普通文件。")
            digest = hashlib.sha256()
            size = 0
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
            actual_digest = "sha256:" + digest.hexdigest()
            if size != receipt["size"] or actual_digest != receipt["sha256"]:
                raise ConnectorIntegrityError("Connector 输出在交付前完整性校验失败。")
            handle.seek(0)
            return receipt, handle
        except Exception:
            handle.close()
            raise

    def collect_results(self, external_run_id: str) -> dict[str, Any]:
        run = self.reconcile(external_run_id)
        if run.get("status") != "succeeded":
            raise ConnectorError(
                "CONNECTOR_RUN_NOT_SUCCEEDED",
                "只有 succeeded 的 AnalysisRun 可以收取结果。",
                retryable=run.get("status") not in TERMINAL_STATUSES,
                details={"status": run.get("status")},
            )
        run_id = str(run["id"])
        manifest = self.client.list_outputs(run_id)
        if set(manifest) != {
            "run_id",
            "execution_status",
            "output_status",
            "error",
            "results",
        } or manifest.get("run_id") != run_id:
            raise ConnectorIntegrityError("输出清单 run_id 与任务不一致。")
        if (
            manifest.get("execution_status") != "succeeded"
            or manifest.get("output_status") != "complete"
            or manifest.get("error") is not None
        ):
            raise ConnectorIntegrityError("输出清单不是 complete，拒绝业务入库。")
        results = manifest.get("results")
        if not isinstance(results, list):
            raise ConnectorIntegrityError("输出清单缺少 results 数组。")
        projection: list[dict[str, Any]] = []
        downloaded: list[dict[str, Any]] = []
        delivered_results: list[dict[str, Any]] = []
        output_receipts: list[dict[str, Any]] = []
        output_keys: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                raise ConnectorIntegrityError("输出清单包含非 object 条目。")
            core_fields = {"key", "name", "semantic_type", "wdl_type", "required", "kind"}
            key = str(item.get("key") or "")
            name = str(item.get("name") or "")
            semantic_type = str(item.get("semantic_type") or "")
            wdl_type = str(item.get("wdl_type") or "")
            kind = str(item.get("kind") or "")
            if (
                not core_fields.issubset(item)
                or set(item) - ANALYSIS_OUTPUT_FIELDS
                or not key
                or not name
                or not semantic_type
                or not wdl_type
                or not isinstance(item.get("required"), bool)
                or ("label" in item and not isinstance(item["label"], str))
                or key in output_keys
            ):
                raise ConnectorIntegrityError(
                    "输出条目核心字段无效、包含未知字段或重复 key。"
                )
            output_keys.add(key)
            if kind == "unverifiable":
                raise ConnectorIntegrityError("输出清单包含不可验证条目，拒绝业务入库。")
            if kind == "directory":
                raise ConnectorIntegrityError(
                    "Directory 输出不能直接下载；流程必须先将其打包为 File。"
                )
            normalized = {
                field: item[field]
                for field in (
                    "key",
                    "name",
                    "label",
                    "semantic_type",
                    "wdl_type",
                    "required",
                    "kind",
                    "filename",
                    "content_type",
                )
                if field in item
            }
            if kind == "file":
                expected_digest = str(item.get("sha256") or "")
                expected_size = item.get("size")
                if (
                    not SHA256.fullmatch(expected_digest)
                    or not isinstance(expected_size, int)
                    or isinstance(expected_size, bool)
                    or expected_size < 0
                    or not _nonempty_string(item.get("download_url"))
                    or "value" in item
                    or any(field in item for field in ("reason", "entry_count", "digest"))
                    or (
                        "filename" in item
                        and not _nonempty_string(item.get("filename"))
                    )
                    or (
                        "content_type" in item
                        and not _nonempty_string(item.get("content_type"))
                    )
                ):
                    raise ConnectorIntegrityError("文件输出字段、size、sha256 或下载地址无效。")
                content = self.client.download_output(item)
                actual_digest = "sha256:" + hashlib.sha256(content).hexdigest()
                if len(content) != expected_size or actual_digest != expected_digest:
                    raise ConnectorIntegrityError(
                        "下载结果与输出清单的 size/sha256 不一致。",
                        details={"key": key},
                    )
                path = self._install_output(
                    external_run_id,
                    key=key,
                    digest=expected_digest,
                    content=content,
                )
                output_receipts.append(
                    {
                        "output_key": key,
                        "semantic_type": semantic_type,
                        "size": expected_size,
                        "sha256": expected_digest,
                        "local_path": str(path),
                    }
                )
                normalized.update({"size": expected_size, "sha256": expected_digest})
                delivered = {
                    **normalized,
                    "local_path": str(path),
                    "download_url": self._output_download_url(external_run_id, key),
                }
                downloaded.append(delivered)
            elif kind == "value":
                if "value" not in item or any(
                    field in item
                    for field in (
                        "reason",
                        "filename",
                        "size",
                        "content_type",
                        "sha256",
                        "entry_count",
                        "digest",
                        "download_url",
                    )
                ):
                    raise ConnectorIntegrityError("value 输出字段无效或缺少 value。")
                normalized["value"] = item["value"]
                delivered = normalized
            else:
                raise ConnectorIntegrityError(f"不支持的输出 kind：{kind}")
            projection.append(normalized)
            delivered_results.append(delivered)
        result_manifest = {
            "run_id": run_id,
            "execution_status": manifest.get("execution_status"),
            "output_status": manifest.get("output_status"),
            "error": None,
            "results": sorted(projection, key=lambda item: item["key"]),
        }
        delivered_results.sort(key=lambda item: item["key"])
        result_digest = canonical_digest(result_manifest)
        record = self.store.commit_result(
            external_run_id,
            result_digest,
            result_manifest,
            output_receipts,
        )
        return {
            "external_run_id": external_run_id,
            "run_id": run_id,
            "result_digest": result_digest,
            "manifest": result_manifest,
            "results": delivered_results,
            "outputs": downloaded,
            "record": record.to_dict(),
        }

    def request_export(
        self,
        external_run_id: str,
        *,
        profile: str,
        requires_ack: bool = True,
        retain_until: str | None = None,
    ) -> dict[str, Any]:
        record = self.store.get_order(external_run_id)
        if not record.run_id:
            self.reconcile(external_run_id)
            record = self.store.get_order(external_run_id)
        if not record.run_id:
            raise SubmissionUncertainError(external_run_id)
        export_request = {
            "run_id": record.run_id,
            "target": {"profile": profile},
            "requires_ack": requires_ack,
            "retain_until": retain_until,
        }
        export_request_digest = canonical_digest(export_request)
        record = self.store.claim_export_request(
            external_run_id,
            export_request_digest,
            export_request,
        )
        idempotency_key = "export-" + export_request_digest.removeprefix("sha256:")
        export = self.client.create_artifact_export(
            record.run_id,
            profile=profile,
            idempotency_key=idempotency_key,
            requires_ack=requires_ack,
            retain_until=retain_until,
        )
        export_id = _validate_artifact_export_response(
            export,
            run_id=record.run_id,
            request=record.export_request,
        )
        self.store.set_export(external_run_id, export_id)
        return export

    def complete_export(self, external_run_id: str) -> dict[str, Any]:
        record = self.store.get_order(external_run_id)
        if (
            not record.export_id
            or record.export_request_digest is None
            or record.export_request is None
        ):
            raise ConnectorError(
                "CONNECTOR_EXPORT_NOT_FOUND",
                "MES 任务尚未绑定 Artifact Export。",
            )
        export = self.client.get_artifact_export(record.export_id)
        _validate_artifact_export_response(
            export,
            run_id=str(record.run_id or ""),
            request=record.export_request,
            expected_export_id=record.export_id,
        )
        manifest = export.get("manifest")
        manifest_digest = str(export.get("manifest_digest") or "")
        requires_ack = export.get("requires_ack")
        if (
            export.get("id") != record.export_id
            or export.get("run_id") != record.run_id
            or not isinstance(requires_ack, bool)
        ):
            raise ConnectorIntegrityError("Artifact Export 详情身份无效。")
        if export.get("state") != "succeeded" or not isinstance(manifest, dict):
            raise ConnectorError(
                "CONNECTOR_EXPORT_NOT_READY",
                "Artifact Export 尚未成功固化交付清单。",
                retryable=export.get("state") in {"pending", "exporting"},
                details={"state": export.get("state")},
            )
        if not SHA256.fullmatch(manifest_digest) or canonical_digest(manifest) != manifest_digest:
            raise ConnectorIntegrityError("Artifact Export manifest_digest 校验失败。")
        _validate_artifact_manifest(
            manifest,
            export_id=record.export_id,
            run_id=record.run_id,
            source_manifest_digest=export.get("source_manifest_digest"),
            target=export.get("target"),
        )
        if record.export_manifest_digest not in {None, manifest_digest}:
            raise ConnectorIntegrityError("Webhook 与 Artifact Export 详情的摘要不一致。")
        self.store.set_export_manifest(
            external_run_id,
            export_id=record.export_id,
            manifest_digest=manifest_digest,
        )
        if requires_ack and not export.get("acknowledged_at"):
            receipt = (
                "connector-"
                + hashlib.sha256(
                    f"{external_run_id}:{manifest_digest}".encode()
                ).hexdigest()[:48]
            )
            export = self.client.acknowledge_artifact_export(
                record.export_id,
                manifest_digest=manifest_digest,
                external_receipt=receipt,
            )
            if (
                export.get("id") != record.export_id
                or export.get("run_id") != record.run_id
                or export.get("manifest_digest") != manifest_digest
                or not export.get("acknowledged_at")
            ):
                raise ConnectorIntegrityError("Artifact Export 确认回执无效。")
        return export

    def order_status(self, external_run_id: str) -> dict[str, Any]:
        record: OrderRecord = self.store.get_order(external_run_id)
        outputs = self.store.list_outputs(external_run_id)
        return {
            **record.to_dict(),
            "outputs": [
                {
                    **item,
                    "download_url": self._output_download_url(
                        external_run_id,
                        str(item["output_key"]),
                    ),
                }
                for item in outputs
            ],
        }

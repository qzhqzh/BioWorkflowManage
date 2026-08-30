from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .api import ConnectorError


EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
PRODUCT_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MISSING = object()


class MappingError(ConnectorError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("CONNECTOR_MAPPING_INVALID", message, details=details)


def canonical_digest(value: Any) -> str:
    body = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _lookup(document: dict[str, Any], path: str, *, required: bool) -> Any:
    current: Any = document
    for part in path.split("."):
        if not part or not isinstance(current, dict) or part not in current:
            if required:
                raise MappingError(
                    f"MES 请求缺少字段：{path}",
                    details={"path": path},
                )
            return _MISSING
        current = current[part]
    return current


def _mapping_table(value: Any, *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise MappingError(f"{label} 必须是 object。")
    result: dict[str, dict[str, Any]] = {}
    for target, raw in value.items():
        if not isinstance(target, str) or not FIELD_NAME.fullmatch(target):
            raise MappingError(f"{label} 包含无效目标字段：{target}")
        if not isinstance(raw, dict):
            raise MappingError(f"{label}.{target} 必须是 object。")
        result[target] = dict(raw)
    return result


def _ensure_reference_has_no_credentials(value: Any, *, path: str = "input") -> None:
    if isinstance(value, dict):
        managed_fields = {
            "type",
            "root_alias",
            "relative_path",
            "sha256",
            "identity_digest",
        }
        s3_fields = {
            "type",
            "profile",
            "bucket",
            "key",
            "version_id",
            "etag",
            "size",
            "sha256",
        }
        reference_type = value.get("type")
        if reference_type == "s3_object":
            required = {"type", "profile", "bucket", "key", "size", "sha256"}
            valid = (
                required.issubset(value)
                and not set(value) - s3_fields
                and bool({"version_id", "etag"} & set(value))
                and isinstance(value.get("profile"), str)
                and bool(
                    re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                        value["profile"],
                    )
                )
                and isinstance(value.get("bucket"), str)
                and bool(
                    re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}",
                        value["bucket"],
                    )
                )
                and isinstance(value.get("key"), str)
                and 0 < len(value["key"]) <= 1024
                and isinstance(value.get("size"), int)
                and not isinstance(value.get("size"), bool)
                and value["size"] > 0
                and isinstance(value.get("sha256"), str)
                and bool(SHA256.fullmatch(value["sha256"]))
                and all(
                    isinstance(value[field], str) and 0 < len(value[field]) <= 1024
                    for field in {"version_id", "etag"} & set(value)
                )
            )
        else:
            required = {"root_alias", "relative_path"}
            relative_path = value.get("relative_path")
            reference_path = (
                PurePosixPath(relative_path)
                if isinstance(relative_path, str)
                else None
            )
            valid = (
                required.issubset(value)
                and not set(value) - managed_fields
                and (reference_type is None or reference_type == "managed_path")
                and value.get("root_alias") in {"rawdata", "database"}
                and isinstance(relative_path, str)
                and bool(relative_path)
                and "\\" not in relative_path
                and reference_path is not None
                and not reference_path.is_absolute()
                and ".." not in reference_path.parts
                and str(reference_path) != "."
                and all(
                    isinstance(value[field], str) and bool(SHA256.fullmatch(value[field]))
                    for field in {"sha256", "identity_digest"} & set(value)
                )
            )
        if not valid:
            raise MappingError(
                "输入引用 object 只能使用固定 managed/s3 字段，不能携带凭据或扩展字段。",
                details={"path": path, "fields": sorted(str(key) for key in value)},
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_reference_has_no_credentials(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        parsed = urlsplit(value.strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            raise MappingError(
                "输入引用不能携带 endpoint、凭据、Token 或预签名 URL。",
                details={"path": path},
            )


@dataclass(frozen=True)
class MappedRequest:
    external_run_id: str
    idempotency_key: str
    preflight: dict[str, Any]
    submission: dict[str, Any]
    request_digest: str


@dataclass(frozen=True)
class MappingConfig:
    client_id: str
    analysis_code: str
    contract_version: str
    expected_contract_digest: str
    external_run_id_path: str
    external_analysis_id_path: str | None
    sample_id_path: str
    input_mappings: dict[str, dict[str, Any]]
    metadata_mappings: dict[str, dict[str, Any]]
    database_mappings: dict[str, dict[str, Any]]

    @classmethod
    def from_dict(cls, value: Any) -> "MappingConfig":
        if not isinstance(value, dict):
            raise MappingError("mapping 必须是 object。")
        allowed = {
            "schema_version",
            "client_id",
            "analysis_product",
            "fields",
            "inputs",
            "metadata",
            "database",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise MappingError("mapping 包含未知字段。", details={"fields": unknown})
        if value.get("schema_version") != "1.0.0":
            raise MappingError("mapping.schema_version 必须是 1.0.0。")
        client_id = str(value.get("client_id") or "").strip()
        if not EXTERNAL_ID.fullmatch(client_id):
            raise MappingError("mapping.client_id 格式无效。")
        product = value.get("analysis_product")
        if not isinstance(product, dict) or set(product) - {
            "analysis_code",
            "contract_version",
            "expected_contract_digest",
        }:
            raise MappingError("mapping.analysis_product 格式无效。")
        analysis_code = str(product.get("analysis_code") or "").strip()
        contract_version = str(product.get("contract_version") or "").strip()
        if not PRODUCT_REFERENCE.fullmatch(analysis_code) or not PRODUCT_REFERENCE.fullmatch(
            contract_version
        ):
            raise MappingError("分析产品 code 或 contract_version 格式无效。")
        expected_digest = str(product.get("expected_contract_digest") or "").strip()
        if expected_digest and not SHA256.fullmatch(expected_digest):
            raise MappingError("expected_contract_digest 必须是 sha256 摘要。")
        fields = value.get("fields")
        if not isinstance(fields, dict) or set(fields) - {
            "external_run_id",
            "external_analysis_id",
            "sample_id",
        }:
            raise MappingError("mapping.fields 格式无效。")
        external_run_id_path = str(fields.get("external_run_id") or "").strip()
        sample_id_path = str(fields.get("sample_id") or "").strip()
        external_analysis_id_path = str(
            fields.get("external_analysis_id") or ""
        ).strip()
        if not external_run_id_path or not sample_id_path:
            raise MappingError("external_run_id 和 sample_id 字段映射不能为空。")
        return cls(
            client_id=client_id,
            analysis_code=analysis_code,
            contract_version=contract_version,
            expected_contract_digest=expected_digest,
            external_run_id_path=external_run_id_path,
            external_analysis_id_path=external_analysis_id_path or None,
            sample_id_path=sample_id_path,
            input_mappings=_mapping_table(value.get("inputs"), label="mapping.inputs"),
            metadata_mappings=_mapping_table(
                value.get("metadata", {}), label="mapping.metadata"
            ),
            database_mappings=_mapping_table(
                value.get("database", {}), label="mapping.database"
            ),
        )

    @property
    def analysis_product(self) -> dict[str, str]:
        return {
            "analysis_code": self.analysis_code,
            "contract_version": self.contract_version,
        }

    @staticmethod
    def _mapped_scalar(
        order: dict[str, Any],
        definition: dict[str, Any],
        *,
        label: str,
    ) -> Any:
        allowed = {"path", "required", "default"}
        unknown = sorted(set(definition) - allowed)
        if unknown:
            raise MappingError(f"{label} 包含未知字段。", details={"fields": unknown})
        if "required" in definition and not isinstance(definition["required"], bool):
            raise MappingError(f"{label}.required 必须是 boolean。")
        source_path = str(definition.get("path") or "").strip()
        if not source_path:
            raise MappingError(f"{label}.path 不能为空。")
        required = bool(definition.get("required", False))
        value = _lookup(order, source_path, required=required)
        if value is _MISSING:
            return definition.get("default", _MISSING)
        if isinstance(value, (dict, list)):
            raise MappingError(f"{label} 只能映射 JSON scalar。")
        return value

    def _map_inputs(self, order: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for target, definition in self.input_mappings.items():
            allowed = {"path", "kind", "root_alias", "required", "default"}
            unknown = sorted(set(definition) - allowed)
            if unknown:
                raise MappingError(
                    f"mapping.inputs.{target} 包含未知字段。",
                    details={"fields": unknown},
                )
            if "required" in definition and not isinstance(
                definition["required"], bool
            ):
                raise MappingError(
                    f"mapping.inputs.{target}.required 必须是 boolean。"
                )
            source_path = str(definition.get("path") or "").strip()
            kind = str(definition.get("kind") or "value").strip()
            required = bool(definition.get("required", True))
            value = _lookup(order, source_path, required=required)
            if value is _MISSING:
                value = definition.get("default", _MISSING)
            if value is _MISSING:
                continue
            if kind == "managed_file":
                relative_path = str(value or "").strip().replace("\\", "/")
                path = PurePosixPath(relative_path)
                root_alias = str(definition.get("root_alias") or "").strip()
                if (
                    root_alias not in {"rawdata", "database"}
                    or not relative_path
                    or path.is_absolute()
                    or ".." in path.parts
                ):
                    raise MappingError(
                        f"mapping.inputs.{target} 不是安全的受管相对路径。"
                    )
                result[target] = {
                    "root_alias": root_alias,
                    "relative_path": relative_path,
                }
            elif kind in {"value", "reference"}:
                _ensure_reference_has_no_credentials(value, path=f"inputs.{target}")
                result[target] = value
            else:
                raise MappingError(f"mapping.inputs.{target}.kind 不受支持：{kind}")
        if not result:
            raise MappingError("映射后的 Analysis Request 没有 inputs。")
        return result

    def map_order(self, order: Any) -> MappedRequest:
        if not isinstance(order, dict):
            raise MappingError("MES 请求顶层必须是 object。")
        external_run_id = str(
            _lookup(order, self.external_run_id_path, required=True) or ""
        ).strip()
        sample_id = str(_lookup(order, self.sample_id_path, required=True) or "").strip()
        if not EXTERNAL_ID.fullmatch(external_run_id):
            raise MappingError("映射后的 external_run_id 格式无效。")
        if not EXTERNAL_ID.fullmatch(sample_id):
            raise MappingError("映射后的 sample_id 格式无效。")
        external_analysis_id = ""
        if self.external_analysis_id_path:
            raw_analysis_id = _lookup(
                order,
                self.external_analysis_id_path,
                required=False,
            )
            if raw_analysis_id is not _MISSING:
                external_analysis_id = str(raw_analysis_id or "").strip()
                if external_analysis_id and not EXTERNAL_ID.fullmatch(
                    external_analysis_id
                ):
                    raise MappingError("映射后的 external_analysis_id 格式无效。")

        metadata: dict[str, Any] = {}
        for target, definition in self.metadata_mappings.items():
            mapped = self._mapped_scalar(
                order,
                definition,
                label=f"mapping.metadata.{target}",
            )
            if mapped is not _MISSING:
                metadata[target] = mapped
        database: dict[str, Any] = {}
        for target, definition in self.database_mappings.items():
            if target not in {"reference_id", "panel_id"}:
                raise MappingError(f"不支持的 database 映射目标：{target}")
            mapped = self._mapped_scalar(
                order,
                definition,
                label=f"mapping.database.{target}",
            )
            if mapped is not _MISSING and str(mapped or "").strip():
                database[target] = str(mapped).strip()

        inputs = self._map_inputs(order)
        preflight: dict[str, Any] = {
            "analysis_product": self.analysis_product,
            "inputs": inputs,
            "metadata": metadata,
        }
        if database:
            preflight["database"] = database
        external_ref = {
            "client_id": self.client_id,
            "external_run_id": external_run_id,
            "external_analysis_id": external_analysis_id,
        }
        submission = {
            **preflight,
            "external_ref": external_ref,
            "subject": {"sample_id": sample_id},
        }
        try:
            request_digest = canonical_digest(
                {"kind": "workflow", "request": submission}
            )
        except (TypeError, ValueError) as error:
            raise MappingError("映射结果不是有效的有限 JSON 值。") from error
        return MappedRequest(
            external_run_id=external_run_id,
            idempotency_key=external_run_id,
            preflight=preflight,
            submission=submission,
            request_digest=request_digest,
        )

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from .models import AnalysisRun


def _local_run_path(value: str | Path) -> Path:
    execution_root = Path(settings.ANALYSIS_RUN_EXECUTION_ROOT).resolve()
    local_root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    resolved = Path(value).resolve()
    try:
        relative = resolved.relative_to(execution_root)
    except ValueError:
        relative = resolved.relative_to(local_root)
    local = (local_root / relative).resolve()
    local.relative_to(local_root)
    return local


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _public_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if not isinstance(value, str):
        return value
    roots = {
        str(getattr(settings, name, "") or "").rstrip("/")
        for name in (
            "ANALYSIS_RAWDATA_ROOT",
            "ANALYSIS_RAWDATA_EXECUTION_ROOT",
            "ANALYSIS_DATABASE_ROOT",
            "ANALYSIS_DATABASE_EXECUTION_ROOT",
            "ANALYSIS_RUN_ROOT",
            "ANALYSIS_RUN_EXECUTION_ROOT",
        )
    }
    result = value
    for root in sorted((item for item in roots if item), key=len, reverse=True):
        result = result.replace(root, "<managed-root>")
    return result


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


def _manifest_value(
    run_root: Path,
    *,
    key: str,
    value: Any,
    wdl_type: str,
    contract: dict[str, Any],
) -> list[dict[str, Any]]:
    optional_type = wdl_type.removesuffix("?")
    if value is None:
        return []
    if optional_type.startswith("Array[") and optional_type.endswith("]"):
        subtype = optional_type[6:-1].strip()
        if not isinstance(value, list):
            return []
        items: list[dict[str, Any]] = []
        for index, item in enumerate(value):
            items.extend(
                _manifest_value(
                    run_root,
                    key=f"{key}[{index}]",
                    value=item,
                    wdl_type=subtype,
                    contract=contract,
                )
            )
        return items
    pair = _split_pair_type(optional_type)
    if pair and isinstance(value, dict):
        items = []
        for side, subtype in zip(("left", "right"), pair, strict=True):
            items.extend(
                _manifest_value(
                    run_root,
                    key=f"{key}.{side}",
                    value=value.get(side),
                    wdl_type=subtype,
                    contract=contract,
                )
            )
        return items

    base = {
        "key": key,
        "name": contract.get("name") or key,
        "label": contract.get("label") or contract.get("name") or key,
        "semantic_type": contract.get("semantic_type") or "core.output.unknown",
        "wdl_type": wdl_type,
        "required": bool(contract.get("required", False)),
    }
    if optional_type not in {"File", "Directory"}:
        return [{**base, "kind": "value", "value": value}]
    if not isinstance(value, str):
        return []
    try:
        path = _local_run_path(value)
        path.relative_to(run_root)
        stat = path.stat()
    except (OSError, ValueError):
        return []
    if optional_type == "Directory":
        if not path.is_dir():
            return []
        return [
            {
                **base,
                "kind": "directory",
                "path": str(path),
                "identity": {
                    "mtime_ns": stat.st_mtime_ns,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                },
            }
        ]
    if not path.is_file():
        return []
    return [
        {
            **base,
            "kind": "file",
            "path": str(path),
            "filename": path.name,
            "size": stat.st_size,
            "sha256": _sha256(path),
            "content_type": mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
            "identity": {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "device": stat.st_dev,
                "inode": stat.st_ino,
            },
        }
    ]


def build_output_manifest(
    run: AnalysisRun,
    result: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    contract = run.request_payload.get("integration_output_contract") or []
    if not isinstance(contract, list):
        contract = []
    values = result.get("outputs", result)
    if not isinstance(values, dict):
        values = {}
    run_root = _local_run_path(run.work_directory)
    items: list[dict[str, Any]] = []
    consumed: set[str] = set()
    missing_required: list[dict[str, Any]] = []

    for raw_contract in contract:
        if not isinstance(raw_contract, dict):
            continue
        expected_key = str(raw_contract.get("key") or "")
        if not expected_key:
            continue
        if expected_key not in values or values[expected_key] is None:
            if raw_contract.get("required"):
                missing_required.append(
                    {
                        "key": expected_key,
                        "semantic_type": raw_contract.get("semantic_type"),
                        "label": raw_contract.get("label") or raw_contract.get("name"),
                    }
                )
            continue
        consumed.add(expected_key)
        output_items = _manifest_value(
            run_root,
            key=expected_key,
            value=values[expected_key],
            wdl_type=str(raw_contract.get("wdl_type") or "String"),
            contract=raw_contract,
        )
        if not output_items and raw_contract.get("required"):
            missing_required.append(
                {
                    "key": expected_key,
                    "semantic_type": raw_contract.get("semantic_type"),
                    "label": raw_contract.get("label") or raw_contract.get("name"),
                }
            )
        items.extend(output_items)

    manifest = {
        "schema_version": 1,
        "created_at": timezone.now().isoformat(),
        "items": items,
        "missing_required": missing_required,
        "uncontracted_output_keys": sorted(str(key) for key in set(values) - consumed),
    }
    if missing_required:
        return (
            manifest,
            AnalysisRun.OutputStatus.INCOMPLETE,
            {
                "code": "REQUIRED_OUTPUT_MISSING",
                "category": "application",
                "retryable": False,
                "details": {"missing": missing_required},
            },
        )
    return manifest, AnalysisRun.OutputStatus.COMPLETE, None


def public_output_manifest(run: AnalysisRun) -> list[dict[str, Any]]:
    results = []
    for item in run.output_manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        public = {
            key: _public_value(value)
            for key, value in item.items()
            if key not in {"path", "identity"}
        }
        if item.get("kind") == "file":
            public["download_url"] = (
                f"/api/v1/integration/analysis-runs/{run.id}/outputs"
                f"/download?key={quote(str(item.get('key') or ''), safe='')}"
            )
        results.append(public)
    return results

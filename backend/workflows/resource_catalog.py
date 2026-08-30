from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction

from .integration_outputs import (
    ResourceSnapshotBudget,
    ResourceSnapshotBudgetError,
    _directory_manifest,
)
from .models import AnalysisResourceCatalog, AnalysisResourceCatalogRevision


CATALOG_KEY = "default"
CATALOG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LEGACY_PANEL_BINDINGS = (
    "bed",
    "gene_bed",
    "gene_list",
    "tert_bed",
    "p1q19_bed",
    "druggable_region",
    "cnvkit_db",
)


class ResourceCatalogError(ValueError):
    def __init__(self, code: str, message: str, *, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def catalog_digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_relative_path(value: Any, *, label: str, allow_blank: bool = False) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path and allow_blank:
        return ""
    candidate = PurePosixPath(path)
    if (
        not path
        or "\x00" in path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or path.startswith("~/")
    ):
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_PATH_INVALID",
            f"{label}必须是数据库根目录下的相对路径。",
            details={"path": path},
        )
    return candidate.as_posix()


def _validate_required(items: Any, *, owner: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_INVALID",
            f"{owner}.required 必须是数组。",
        )
    normalized = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_INVALID",
                f"{owner}.required[{index}] 必须是对象。",
            )
        path = _safe_relative_path(
            item.get("path"), label=f"{owner}.required[{index}].path"
        )
        kind = str(item.get("kind") or "file")
        if kind not in {"file", "directory"}:
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_INVALID",
                f"{owner}.required[{index}].kind 只支持 file 或 directory。",
            )
        if path in seen:
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_DUPLICATE",
                f"{owner} 重复声明资源路径：{path}",
            )
        seen.add(path)
        alternatives = item.get("alternatives") or []
        if not isinstance(alternatives, list):
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_INVALID",
                f"{owner}.required[{index}].alternatives 必须是数组。",
            )
        normalized_item = {
            "path": path,
            "kind": kind,
            "label": str(item.get("label") or path).strip()[:256],
        }
        if alternatives:
            normalized_item["alternatives"] = [
                _safe_relative_path(
                    value,
                    label=f"{owner}.required[{index}].alternatives",
                )
                for value in alternatives
            ]
        sha256 = str(item.get("sha256") or "").removeprefix("sha256:")
        if sha256:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                raise ResourceCatalogError(
                    "RESOURCE_CATALOG_INVALID",
                    f"{owner}.required[{index}].sha256 格式无效。",
                )
            normalized_item["sha256"] = sha256.lower()
        identity_digest = str(item.get("identity_digest") or "").removeprefix(
            "sha256:"
        )
        if identity_digest:
            if kind != "directory" or not re.fullmatch(
                r"[0-9a-fA-F]{64}", identity_digest
            ):
                raise ResourceCatalogError(
                    "RESOURCE_CATALOG_INVALID",
                    f"{owner}.required[{index}].identity_digest 仅支持 64 位目录身份摘要。",
                )
            normalized_item["identity_digest"] = identity_digest.lower()
        normalized.append(normalized_item)
    return normalized


def _validate_entry(item: Any, *, kind: str, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_INVALID", f"{kind}[{index}] 必须是对象。"
        )
    identifier = str(item.get("id") or "").strip()
    if not CATALOG_ID.fullmatch(identifier):
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_INVALID",
            f"{kind}[{index}].id 格式无效。",
        )
    normalized = copy.deepcopy(item)
    normalized["id"] = identifier
    normalized["name"] = str(item.get("name") or identifier).strip()[:256]
    normalized["description"] = str(item.get("description") or "").strip()[:2000]
    normalized["required"] = _validate_required(
        item.get("required") or [], owner=f"{kind}.{identifier}"
    )
    directories = item.get("directories") or {}
    if not isinstance(directories, dict):
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_INVALID",
            f"{kind}.{identifier}.directories 必须是对象。",
        )
    normalized["directories"] = {
        str(key): _safe_relative_path(
            value, label=f"{kind}.{identifier}.directories.{key}"
        )
        for key, value in directories.items()
    }
    bindings = item.get("bindings") or {}
    if not isinstance(bindings, dict):
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_INVALID",
            f"{kind}.{identifier}.bindings 必须是对象。",
        )
    normalized_bindings = {}
    for key, value in bindings.items():
        if isinstance(value, list):
            normalized_bindings[str(key)] = [
                _safe_relative_path(
                    path,
                    label=f"{kind}.{identifier}.bindings.{key}",
                    allow_blank=True,
                )
                for path in value
            ]
        else:
            normalized_bindings[str(key)] = _safe_relative_path(
                value,
                label=f"{kind}.{identifier}.bindings.{key}",
                allow_blank=True,
            )
    normalized["bindings"] = normalized_bindings
    for key in LEGACY_PANEL_BINDINGS:
        if key in item:
            normalized[key] = _safe_relative_path(
                item.get(key),
                label=f"{kind}.{identifier}.{key}",
                allow_blank=True,
            )
    required_bindings = item.get("required_bindings") or []
    if not isinstance(required_bindings, list):
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_INVALID",
            f"{kind}.{identifier}.required_bindings 必须是数组。",
        )
    normalized["required_bindings"] = []
    binding_keys = set()
    for binding in required_bindings:
        if not isinstance(binding, dict) or not str(binding.get("key") or "").strip():
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_INVALID",
                f"{kind}.{identifier}.required_bindings 存在无效项。",
            )
        key = str(binding["key"]).strip()
        binding_kind = str(binding.get("kind") or "file")
        basename_includes = binding.get("basename_includes") or []
        if key in binding_keys:
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_DUPLICATE",
                f"{kind}.{identifier}.required_bindings 重复声明 {key}。",
            )
        if binding_kind not in {"file", "directory"}:
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_INVALID",
                f"{kind}.{identifier}.required_bindings.{key}.kind 只支持 file 或 directory。",
            )
        if not isinstance(basename_includes, list) or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 64
            or "/" in value
            or "\x00" in value
            for value in basename_includes
        ):
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_INVALID",
                f"{kind}.{identifier}.required_bindings.{key}.basename_includes 格式无效。",
            )
        binding_keys.add(key)
        normalized_binding = {
            "key": key,
            "label": str(binding.get("label") or key).strip()[:256],
            "kind": binding_kind,
        }
        if basename_includes:
            normalized_binding["basename_includes"] = [
                value.strip() for value in basename_includes
            ]
        normalized["required_bindings"].append(normalized_binding)
    workflow_ids = item.get("workflow_ids") or []
    if not isinstance(workflow_ids, list):
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_INVALID",
            f"{kind}.{identifier}.workflow_ids 必须是数组。",
        )
    normalized["workflow_ids"] = [
        str(value).strip() for value in workflow_ids if str(value).strip()
    ]
    if kind == "panels":
        normalized["reference"] = str(item.get("reference") or "").strip()
    return normalized


def validate_catalog(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_INVALID",
            "资源目录必须是 schema_version 1 的 JSON 对象。",
        )
    normalized: dict[str, Any] = {"schema_version": 1}
    for kind in ("references", "panels"):
        entries = document.get(kind)
        if not isinstance(entries, list):
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_INVALID", f"{kind} 必须是数组。"
            )
        normalized[kind] = [
            _validate_entry(item, kind=kind, index=index)
            for index, item in enumerate(entries)
        ]
        identifiers = [item["id"] for item in normalized[kind]]
        if len(identifiers) != len(set(identifiers)):
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_DUPLICATE", f"{kind} 中存在重复 id。"
            )
    references = {item["id"] for item in normalized["references"]}
    for panel in normalized["panels"]:
        if panel["reference"] and panel["reference"] not in references:
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_REFERENCE_MISSING",
                f"Panel {panel['id']} 引用了不存在的参考版本 {panel['reference']}。",
            )
    return normalized


def _file_catalog() -> dict[str, Any]:
    path = Path(settings.ANALYSIS_DATABASE_CATALOG)
    if not path.is_file():
        raise ResourceCatalogError(
            "ANALYSIS_DATABASE_CATALOG_MISSING",
            "数据库 catalog.json 尚未就绪。",
            details={"path": "workspace/databases/catalog.json"},
        )
    try:
        return validate_catalog(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise ResourceCatalogError(
            "ANALYSIS_DATABASE_CATALOG_INVALID",
            f"数据库 catalog.json 无法读取：{error}",
        ) from error


def load_catalog_state() -> dict[str, Any]:
    state = AnalysisResourceCatalog.objects.filter(key=CATALOG_KEY).first()
    if state is not None:
        document = validate_catalog(state.document)
        digest = catalog_digest(document)
        if digest != state.digest:
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_DIGEST_MISMATCH",
                "数据库中的资源目录摘要不匹配，请先修复目录状态。",
            )
        return {
            "document": document,
            "version": state.version,
            "digest": state.digest,
            "source": "managed",
            "updated_by": state.updated_by,
            "updated_at": state.updated_at,
        }
    document = _file_catalog()
    return {
        "document": document,
        "version": 0,
        "digest": catalog_digest(document),
        "source": "file",
        "updated_by": "catalog.json",
        "updated_at": None,
    }


def load_active_catalog() -> dict[str, Any]:
    return load_catalog_state()["document"]


def _entry_map(document: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in document[key]}


def catalog_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for kind in ("references", "panels"):
        old = _entry_map(before, kind)
        new = _entry_map(after, kind)
        changes[kind] = {
            "created": sorted(set(new) - set(old)),
            "updated": sorted(
                identifier
                for identifier in set(old) & set(new)
                if old[identifier] != new[identifier]
            ),
            "deleted": sorted(set(old) - set(new)),
        }
    return changes


@transaction.atomic
def save_catalog(
    document: Any,
    *,
    base_version: int,
    base_digest: str,
    actor: str,
    note: str,
) -> AnalysisResourceCatalog:
    normalized = validate_catalog(document)
    state = (
        AnalysisResourceCatalog.objects.select_for_update()
        .filter(key=CATALOG_KEY)
        .first()
    )
    if state is None:
        current = _file_catalog()
        current_version = 0
        current_digest = catalog_digest(current)
    else:
        current = validate_catalog(state.document)
        current_version = state.version
        current_digest = state.digest
        if catalog_digest(current) != current_digest:
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_DIGEST_MISMATCH",
                "数据库中的资源目录摘要不匹配，请先修复目录状态。",
            )
    if base_version != current_version or base_digest != current_digest:
        raise ResourceCatalogError(
            "RESOURCE_CATALOG_CONFLICT",
            "资源目录已被其他用户修改，请刷新后再保存。",
            details={
                "current_version": current_version,
                "current_digest": current_digest,
                "updated_by": state.updated_by if state else "catalog.json",
            },
        )
    digest = catalog_digest(normalized)
    if digest == current_digest:
        if state is None:
            try:
                with transaction.atomic():
                    state = AnalysisResourceCatalog.objects.create(
                        key=CATALOG_KEY,
                        document=normalized,
                        version=1,
                        digest=digest,
                        created_by=actor,
                        updated_by=actor,
                    )
            except IntegrityError as error:
                raise ResourceCatalogError(
                    "RESOURCE_CATALOG_CONFLICT",
                    "资源目录已被其他用户纳管，请刷新后再保存。",
                ) from error
            AnalysisResourceCatalogRevision.objects.create(
                catalog=state,
                version=1,
                digest=digest,
                document=normalized,
                actor=actor,
                note=note or "纳管现有 catalog.json。",
                changes=catalog_changes({"references": [], "panels": []}, normalized),
            )
        return state
    next_version = current_version + 1
    if state is None:
        try:
            with transaction.atomic():
                state = AnalysisResourceCatalog.objects.create(
                    key=CATALOG_KEY,
                    document=normalized,
                    version=next_version,
                    digest=digest,
                    created_by=actor,
                    updated_by=actor,
                )
        except IntegrityError as error:
            raise ResourceCatalogError(
                "RESOURCE_CATALOG_CONFLICT",
                "资源目录已被其他用户纳管，请刷新后再保存。",
            ) from error
    else:
        state.document = normalized
        state.version = next_version
        state.digest = digest
        state.updated_by = actor
        state.save(
            update_fields=[
                "document",
                "version",
                "digest",
                "updated_by",
                "updated_at",
            ]
        )
    AnalysisResourceCatalogRevision.objects.create(
        catalog=state,
        version=next_version,
        digest=digest,
        document=normalized,
        actor=actor,
        note=note,
        changes=catalog_changes(current, normalized),
    )
    return state


def entry_binding(entry: dict[str, Any], name: str) -> Any:
    bindings = entry.get("bindings")
    if isinstance(bindings, dict) and name in bindings:
        return bindings[name]
    return entry.get(name)


def entry_requirements(
    entry: dict[str, Any],
    *,
    verify_checksums: bool = False,
    snapshot_budget: Any | None = None,
) -> list[dict[str, Any]]:
    root = Path(settings.ANALYSIS_DATABASE_ROOT)
    if verify_checksums and snapshot_budget is None:
        snapshot_budget = ResourceSnapshotBudget()
    results = []
    for resource_index, item in enumerate(catalog_resource_specs(entry)):
        relative_path = str(item.get("path") or "")
        kind = str(item.get("kind") or "file")
        candidates = [
            relative_path,
            *(str(path) for path in item.get("alternatives", [])),
        ]
        present = False
        checksum_mismatch = False
        observed_identity_digest = ""
        for candidate_index, candidate_path in enumerate(candidates):
            if snapshot_budget is not None:
                snapshot_budget.claim_unique(
                    f"catalog:{resource_index}:{candidate_index}:{kind}:{candidate_path}"
                )
            try:
                safe = _safe_relative_path(candidate_path, label="resource path")
                candidate = (root.resolve() / safe).resolve()
                candidate.relative_to(root.resolve())
                present = (
                    candidate.is_dir() if kind == "directory" else candidate.is_file()
                )
                checksum = (
                    item.get("identity_digest")
                    if kind == "directory"
                    else item.get("sha256")
                )
                if present and verify_checksums and kind == "directory":
                    directory_manifest = (
                        snapshot_budget.directory_manifest(
                            candidate,
                            containment_root=root,
                        )
                        if snapshot_budget is not None
                        else _directory_manifest(candidate)
                    )
                    observed_identity_digest = directory_manifest["digest"]
                    actual_digest = observed_identity_digest.removeprefix("sha256:")
                    if checksum:
                        checksum_mismatch = actual_digest != checksum
                        present = not checksum_mismatch
                elif present and checksum and verify_checksums:
                    actual_digest = snapshot_budget.file_digest(
                        candidate,
                        containment_root=root.resolve(),
                    ).removeprefix("sha256:")
                    checksum_mismatch = actual_digest != checksum
                    present = not checksum_mismatch
            except ResourceSnapshotBudgetError:
                raise
            except (OSError, ValueError, ResourceCatalogError):
                present = False
            if present:
                break
        result = {
                "path": relative_path,
                "label": str(item.get("label") or relative_path),
                "kind": kind,
                "present": present,
                "warning": (
                    "legacy_directory_sha256_ignored"
                    if kind == "directory"
                    and item.get("sha256")
                    and not item.get("identity_digest")
                    else ""
                ),
                "reason": (
                    "checksum_mismatch"
                    if checksum_mismatch
                    else "missing"
                    if not present
                    else ""
                ),
            }
        if observed_identity_digest:
            result["observed_identity_digest"] = observed_identity_digest
        results.append(result)
    configured_binding_keys = {
        str(item.get("binding") or "") for item in results if item.get("binding")
    }
    for binding in entry.get("required_bindings", []):
        key = str(binding.get("key") or "")
        value = entry_binding(entry, key)
        configured = bool(value) and (not isinstance(value, list) or all(value))
        basename_includes = binding.get("basename_includes") or []
        values = value if isinstance(value, list) else [value]
        if configured and basename_includes and any(
            not any(token in PurePosixPath(str(path)).name for token in basename_includes)
            for path in values
        ):
            results.append(
                {
                    "path": str(value) if not isinstance(value, list) else "",
                    "binding": key,
                    "label": f"{binding.get('label') or key}文件名规则",
                    "kind": "configuration",
                    "present": False,
                    "reason": "constraint_mismatch",
                    "expected": basename_includes,
                }
            )
        if configured or key in configured_binding_keys:
            continue
        results.append(
            {
                "path": "",
                "binding": key,
                "label": str(binding.get("label") or key),
                "kind": "configuration",
                "present": False,
                "reason": "unconfigured",
            }
        )
    return results


def catalog_resource_specs(entry: dict[str, Any]) -> list[dict[str, Any]]:
    resources = [copy.deepcopy(item) for item in entry.get("required", [])]
    seen = {str(item.get("path") or "") for item in resources}
    for binding in entry.get("required_bindings", []):
        key = str(binding.get("key") or "")
        value = entry_binding(entry, key)
        values = value if isinstance(value, list) else [value]
        for path in values:
            relative_path = str(path or "")
            if not relative_path or relative_path in seen:
                continue
            seen.add(relative_path)
            resources.append(
                {
                    "path": relative_path,
                    "kind": str(binding.get("kind") or "file"),
                    "label": str(binding.get("label") or key),
                    "binding": key,
                }
            )
    return resources


def entry_status(
    entry: dict[str, Any],
    *,
    verify_checksums: bool = False,
    snapshot_budget: ResourceSnapshotBudget | None = None,
) -> dict[str, Any]:
    requirements = entry_requirements(
        entry,
        verify_checksums=verify_checksums,
        snapshot_budget=snapshot_budget,
    )
    missing = [item for item in requirements if not item["present"]]
    return {
        "ready": not missing,
        "requirements": requirements,
        "missing": missing,
    }


def catalog_payload(
    *, verify_entry: tuple[str, str] | None = None
) -> dict[str, Any]:
    state = load_catalog_state()
    document = state["document"]
    revisions = []
    if state["source"] == "managed":
        revisions = [
            {
                "version": item.version,
                "digest": item.digest,
                "actor": item.actor,
                "note": item.note,
                "changes": item.changes,
                "created_at": item.created_at,
            }
            for item in AnalysisResourceCatalogRevision.objects.filter(
                catalog__key=CATALOG_KEY
            )[:30]
        ]
    snapshot_budget = ResourceSnapshotBudget() if verify_entry is not None else None
    references = [
        {
            **item,
            **entry_status(
                item,
                verify_checksums=verify_entry == ("references", item["id"]),
                snapshot_budget=(
                    snapshot_budget
                    if verify_entry == ("references", item["id"])
                    else None
                ),
            ),
        }
        for item in document["references"]
    ]
    panels = [
        {
            **item,
            **entry_status(
                item,
                verify_checksums=verify_entry == ("panels", item["id"]),
                snapshot_budget=(
                    snapshot_budget
                    if verify_entry == ("panels", item["id"])
                    else None
                ),
            ),
        }
        for item in document["panels"]
    ]
    return {
        **state,
        "document": document,
        "references": references,
        "panels": panels,
        "summary": {
            "reference_count": len(references),
            "ready_reference_count": sum(item["ready"] for item in references),
            "panel_count": len(panels),
            "ready_panel_count": sum(item["ready"] for item in panels),
            "missing_count": sum(
                len(item["missing"]) for item in [*references, *panels]
            ),
        },
        "revisions": revisions,
    }

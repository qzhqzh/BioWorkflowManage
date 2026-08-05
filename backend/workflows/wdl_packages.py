from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import WDL


MAX_PACKAGE_FILES = 256
MAX_PACKAGE_CONTENT_LENGTH = 10_000_000
MAX_ARCHIVE_LENGTH = 5_000_000


class WDLPackageError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def package_digest(files: dict[str, str]) -> str:
    canonical = "".join(
        f"{path}\0{digest(content)}\n" for path, content in sorted(files.items())
    )
    return digest(canonical)


def normalize_newlines(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def normalize_package_path(value: str, *, require_wdl: bool = True) -> str:
    candidate = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(candidate)
    parts = [part for part in path.parts if part not in {"", "."}]
    if (
        not candidate
        or path.is_absolute()
        or not parts
        or any(part == ".." for part in parts)
        or "\x00" in candidate
    ):
        raise WDLPackageError(
            "WDL_PACKAGE_PATH_INVALID",
            f"Invalid WDL package path: {candidate or '<empty>'}.",
        )
    normalized = "/".join(parts)
    if len(normalized) > 512 or (require_wdl and not normalized.lower().endswith(".wdl")):
        raise WDLPackageError(
            "WDL_PACKAGE_PATH_INVALID",
            f"Invalid WDL package path: {normalized}.",
        )
    return normalized


def normalize_bundle_files(raw_files: list[dict], entrypoint: str) -> tuple[dict[str, str], str]:
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_PACKAGE_FILES:
        raise WDLPackageError(
            "WDL_PACKAGE_FILES_INVALID",
            f"A WDL package must contain between 1 and {MAX_PACKAGE_FILES} files.",
        )
    files: dict[str, str] = {}
    casefold_paths: set[str] = set()
    total_length = 0
    for item in raw_files:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise WDLPackageError(
                "WDL_PACKAGE_FILES_INVALID",
                "Each WDL package file requires path and content.",
            )
        path = normalize_package_path(item.get("path", ""))
        folded = path.casefold()
        if folded in casefold_paths:
            raise WDLPackageError(
                "WDL_PACKAGE_PATH_CONFLICT",
                f"Duplicate WDL package path: {path}.",
            )
        content = normalize_newlines(item["content"])
        if not content.strip():
            raise WDLPackageError(
                "WDL_PACKAGE_CONTENT_INVALID",
                f"WDL package file is empty: {path}.",
            )
        total_length += len(content)
        if total_length > MAX_PACKAGE_CONTENT_LENGTH:
            raise WDLPackageError(
                "WDL_PACKAGE_TOO_LARGE",
                "WDL package content exceeds the 10,000,000 character limit.",
            )
        files[path] = content
        casefold_paths.add(folded)
    normalized_entrypoint = normalize_package_path(entrypoint)
    if normalized_entrypoint not in files:
        raise WDLPackageError(
            "WDL_ENTRYPOINT_NOT_FOUND",
            f"Entrypoint is not present in the package: {normalized_entrypoint}.",
        )
    return files, normalized_entrypoint


def _entrypoint_candidates(files: dict[str, str]) -> list[str]:
    workflow_pattern = re.compile(r"(?m)^\s*workflow\s+[A-Za-z_][A-Za-z0-9_]*\s*\{")
    return sorted(path for path, content in files.items() if workflow_pattern.search(content))


def read_wdl_archive(uploaded, requested_entrypoint: str = "") -> tuple[dict[str, str], str]:
    archive = uploaded.read(MAX_ARCHIVE_LENGTH + 1)
    if len(archive) > MAX_ARCHIVE_LENGTH:
        raise WDLPackageError(
            "WDL_ARCHIVE_TOO_LARGE",
            "WDL archive exceeds the 5 MB upload limit.",
        )
    try:
        package = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as error:
        raise WDLPackageError("WDL_ARCHIVE_INVALID", "The uploaded ZIP is invalid.") from error

    files: dict[str, str] = {}
    manifest: dict[str, Any] = {}
    infos = [item for item in package.infolist() if not item.is_dir()]
    if len(infos) > MAX_PACKAGE_FILES + 1:
        raise WDLPackageError(
            "WDL_PACKAGE_FILES_INVALID",
            f"A WDL package may contain at most {MAX_PACKAGE_FILES} WDL files.",
        )
    total_length = 0
    for info in infos:
        raw_path = info.filename.replace("\\", "/")
        if raw_path.casefold().endswith("manifest.json"):
            if posixpath.basename(raw_path).casefold() == "manifest.json":
                try:
                    manifest = json.loads(package.read(info).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    manifest = {}
            continue
        if not raw_path.lower().endswith(".wdl"):
            continue
        path = normalize_package_path(raw_path)
        try:
            content = normalize_newlines(package.read(info).decode("utf-8"))
        except UnicodeDecodeError as error:
            raise WDLPackageError(
                "WDL_PACKAGE_ENCODING_INVALID",
                f"WDL package file is not UTF-8: {path}.",
            ) from error
        total_length += len(content)
        if total_length > MAX_PACKAGE_CONTENT_LENGTH:
            raise WDLPackageError(
                "WDL_PACKAGE_TOO_LARGE",
                "WDL package content exceeds the 10,000,000 character limit.",
            )
        if path.casefold() in {item.casefold() for item in files}:
            raise WDLPackageError(
                "WDL_PACKAGE_PATH_CONFLICT",
                f"Duplicate WDL package path: {path}.",
            )
        files[path] = content
    if not files:
        raise WDLPackageError(
            "WDL_ARCHIVE_EMPTY",
            "The uploaded ZIP does not contain any .wdl files.",
        )

    entrypoint = requested_entrypoint.strip()
    if not entrypoint:
        entrypoint = str(
            manifest.get("mainWorkflowURL")
            or manifest.get("mainWorkflowUrl")
            or manifest.get("main_workflow_url")
            or ""
        )
    if not entrypoint:
        candidates = _entrypoint_candidates(files)
        root_candidates = [item for item in candidates if "/" not in item]
        selected = root_candidates if len(root_candidates) == 1 else candidates
        if len(selected) != 1:
            raise WDLPackageError(
                "WDL_ENTRYPOINT_REQUIRED",
                "The package has multiple workflow entrypoints; select one before importing.",
                details={"candidates": candidates},
            )
        entrypoint = selected[0]
    normalized_files, normalized_entrypoint = normalize_bundle_files(
        [{"path": path, "content": content} for path, content in files.items()],
        entrypoint,
    )
    return normalized_files, normalized_entrypoint


def read_wdl_library_archive(uploaded) -> dict[str, str]:
    archive = uploaded.read(MAX_ARCHIVE_LENGTH + 1)
    if len(archive) > MAX_ARCHIVE_LENGTH:
        raise WDLPackageError(
            "WDL_ARCHIVE_TOO_LARGE",
            "WDL archive exceeds the 5 MB upload limit.",
        )
    try:
        package = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as error:
        raise WDLPackageError("WDL_ARCHIVE_INVALID", "The uploaded ZIP is invalid.") from error

    raw_files = []
    infos = [item for item in package.infolist() if not item.is_dir()]
    if len(infos) > MAX_PACKAGE_FILES + 1:
        raise WDLPackageError(
            "WDL_PACKAGE_FILES_INVALID",
            f"A WDL package may contain at most {MAX_PACKAGE_FILES} WDL files.",
        )
    for info in infos:
        raw_path = info.filename.replace("\\", "/")
        if not raw_path.lower().endswith(".wdl"):
            continue
        path = normalize_package_path(raw_path)
        try:
            content = package.read(info).decode("utf-8")
        except UnicodeDecodeError as error:
            raise WDLPackageError(
                "WDL_PACKAGE_ENCODING_INVALID",
                f"WDL package file is not UTF-8: {path}.",
            ) from error
        raw_files.append({"path": path, "content": content})
    if not raw_files:
        raise WDLPackageError(
            "WDL_ARCHIVE_EMPTY",
            "The uploaded ZIP does not contain any .wdl files.",
        )
    files, _ = normalize_bundle_files(raw_files, raw_files[0]["path"])
    return files


def _source_position(item) -> dict:
    position = getattr(item, "pos", None)
    if position is None:
        return {}
    return {
        "line": getattr(position, "line", None),
        "column": getattr(position, "column", None),
        "end_line": getattr(position, "end_line", None),
        "end_column": getattr(position, "end_column", None),
    }


def _declaration_payload(declaration) -> dict:
    wdl_type = str(getattr(declaration, "type", ""))
    return {
        "name": declaration.name,
        "type": wdl_type,
        "optional": wdl_type.endswith("?"),
        "has_default": getattr(declaration, "expr", None) is not None,
        **_source_position(declaration),
    }


def _diagnostic_from_error(error: Exception, code: str, file_path: str) -> dict:
    position = getattr(error, "pos", None)
    location = {}
    if position is not None:
        location = {
            "line": getattr(position, "line", None),
            "column": getattr(position, "column", None),
        }
    return {
        "code": code,
        "stage": "wdl_analysis",
        "severity": "error",
        "message": str(error).strip() or type(error).__name__,
        "file_path": file_path,
        **({"location": location} if any(location.values()) else {}),
    }


def _validation_errors(error: Exception) -> list[Exception]:
    nested = getattr(error, "exceptions", None) or getattr(error, "_exceptions", None)
    return list(nested) if nested else [error]


def _slice_source(content: str, item) -> str:
    position = getattr(item, "pos", None)
    if not position or not position.line or not position.end_line:
        return ""
    lines = content.splitlines(keepends=True)
    return "".join(lines[position.line - 1 : position.end_line])


def _resolve_import(importer_path: str, uri: str) -> tuple[str | None, str]:
    parsed = urlparse(uri)
    if parsed.scheme or uri.startswith("/"):
        return None, "external"
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(importer_path), uri))
    if normalized == ".." or normalized.startswith("../"):
        return None, "outside"
    try:
        return normalize_package_path(normalized), "local"
    except WDLPackageError:
        return None, "invalid"


def _document_payload(path: str, content: str) -> tuple[dict, Any | None]:
    diagnostics: list[dict] = []
    try:
        document = WDL.parse_document(content, uri=path)
    except Exception as error:
        diagnostics.append(_diagnostic_from_error(error, "WDL_PARSE_ERROR", path))
        return {
            "path": path,
            "digest": digest(content),
            "status": "invalid",
            "parsed": False,
            "wdl_version": None,
            "task_count": 0,
            "workflow_count": 0,
            "import_count": 0,
            "diagnostics": diagnostics,
        }, None

    if not document.imports:
        try:
            document.typecheck()
        except Exception as error:
            diagnostics.extend(
                _diagnostic_from_error(item, "WDL_TYPE_ERROR", path)
                for item in _validation_errors(error)
            )
    return {
        "path": path,
        "digest": digest(content),
        "status": "invalid" if diagnostics else "valid",
        "parsed": True,
        "wdl_version": str(document.wdl_version),
        "task_count": len(document.tasks),
        "workflow_count": 1 if document.workflow is not None else 0,
        "import_count": len(document.imports),
        "diagnostics": diagnostics,
    }, document


def _bundle_typecheck(files: dict[str, str], entrypoint: str) -> list[dict]:
    try:
        with tempfile.TemporaryDirectory(prefix="bioworkflow-wdl-package-") as temp_dir:
            root = Path(temp_dir)
            for path, content in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
            WDL.load(str(root / entrypoint))
        return []
    except Exception as error:
        deepest = error
        while deepest.__cause__ is not None:
            deepest = deepest.__cause__
        position = getattr(deepest, "pos", None)
        absolute_path = str(getattr(position, "abspath", "") or "")
        file_path = entrypoint
        if absolute_path:
            try:
                file_path = str(Path(absolute_path).relative_to(root)).replace("\\", "/")
            except (ValueError, UnboundLocalError):
                pass
        return [_diagnostic_from_error(deepest, "WDL_BUNDLE_TYPE_ERROR", file_path)]


def analyze_wdl_bundle(files: dict[str, str], entrypoint: str) -> dict:
    file_results: dict[str, dict] = {}
    documents: dict[str, Any] = {}
    for path, content in sorted(files.items()):
        file_result, document = _document_payload(path, content)
        file_results[path] = file_result
        if document is not None:
            documents[path] = document

    imports: list[dict] = []
    import_targets: dict[str, list[str]] = {}
    for path, document in documents.items():
        targets: list[str] = []
        for item in document.imports:
            target, kind = _resolve_import(path, item.uri)
            state = "external" if kind == "external" else "resolved" if target in files else "missing"
            imports.append(
                {
                    "file_path": path,
                    "uri": item.uri,
                    "namespace": item.namespace,
                    "target_path": target,
                    "status": state,
                    **_source_position(item),
                }
            )
            if state == "resolved" and target:
                targets.append(target)
        import_targets[path] = targets

    reachable: set[str] = set()
    queue = [entrypoint]
    while queue:
        path = queue.pop(0)
        if path in reachable:
            continue
        reachable.add(path)
        queue.extend(import_targets.get(path, []))

    tasks: list[dict] = []
    workflows: list[dict] = []
    for path in sorted(reachable):
        document = documents.get(path)
        if document is None:
            continue
        content = files[path]
        for task in document.tasks:
            source = _slice_source(content, task)
            tasks.append(
                {
                    "id": f"{path}::{task.name}",
                    "name": task.name,
                    "file_path": path,
                    "source_digest": digest(source),
                    **_source_position(task),
                    "inputs": [_declaration_payload(item) for item in (task.inputs or [])],
                    "outputs": [_declaration_payload(item) for item in (task.outputs or [])],
                    "runtime_keys": sorted((task.runtime or {}).keys()),
                }
            )
        if document.workflow is not None:
            body_counts: dict[str, int] = {}
            for item in getattr(document.workflow, "body", []):
                key = type(item).__name__.lower()
                body_counts[key] = body_counts.get(key, 0) + 1
            workflows.append(
                {
                    "name": document.workflow.name,
                    "file_path": path,
                    **_source_position(document.workflow),
                    "inputs": [
                        _declaration_payload(item)
                        for item in (getattr(document.workflow, "inputs", None) or [])
                    ],
                    "outputs": [
                        _declaration_payload(item)
                        for item in (getattr(document.workflow, "outputs", None) or [])
                    ],
                    "structure": {
                        "call_count": body_counts.get("call", 0),
                        "scatter_count": body_counts.get("scatter", 0),
                        "conditional_count": body_counts.get("conditional", 0),
                    },
                }
            )

    diagnostics = [
        item
        for path in reachable
        for item in file_results.get(path, {}).get("diagnostics", [])
    ]
    diagnostics.extend(
        {
            "code": "WDL_IMPORT_MISSING",
            "stage": "wdl_analysis",
            "severity": "error",
            "message": f"Imported WDL is not present in the package: {item['uri']}.",
            "file_path": item["file_path"],
            **({"location": {"line": item.get("line"), "column": item.get("column")}} if item.get("line") else {}),
        }
        for item in imports
        if item["file_path"] in reachable and item["status"] == "missing"
    )
    package_diagnostics = _bundle_typecheck(files, entrypoint)
    existing = {
        (item["code"], item.get("file_path"), item.get("location", {}).get("line"))
        for item in diagnostics
    }
    diagnostics.extend(
        item
        for item in package_diagnostics
        if (item["code"], item.get("file_path"), item.get("location", {}).get("line")) not in existing
    )
    for path, item in file_results.items():
        item["reachable"] = path in reachable

    version = file_results.get(entrypoint, {}).get("wdl_version")
    return {
        "status": "invalid" if diagnostics else "valid",
        "parsed": bool(file_results.get(entrypoint, {}).get("parsed")),
        "wdl_version": version,
        "package": {
            "entrypoint": entrypoint,
            "file_count": len(files),
            "reachable_file_count": len(reachable),
            "orphan_file_count": len(files) - len(reachable),
            "resolved_import_count": sum(item["status"] == "resolved" for item in imports if item["file_path"] in reachable),
            "missing_import_count": sum(item["status"] == "missing" for item in imports if item["file_path"] in reachable),
        },
        "summary": {
            "task_count": len(tasks),
            "workflow_count": len(workflows),
            "import_count": sum(item["file_path"] in reachable for item in imports),
            "error_count": len(diagnostics),
        },
        "files": [file_results[path] for path in sorted(file_results)],
        "imports": [item for item in imports if item["file_path"] in reachable],
        "tasks": tasks,
        "workflows": workflows,
        "diagnostics": diagnostics,
    }


def analyze_wdl_library(files: dict[str, str]) -> dict:
    file_results: dict[str, dict] = {}
    documents: dict[str, Any] = {}
    for path, content in sorted(files.items()):
        file_result, document = _document_payload(path, content)
        file_result["reachable"] = True
        file_results[path] = file_result
        if document is not None:
            documents[path] = document

    imports: list[dict] = []
    local_targets: set[str] = set()
    roots_with_external_imports: set[str] = set()
    for path, document in documents.items():
        for item in document.imports:
            target, kind = _resolve_import(path, item.uri)
            state = "external" if kind == "external" else "resolved" if target in files else "missing"
            imports.append(
                {
                    "file_path": path,
                    "uri": item.uri,
                    "namespace": item.namespace,
                    "target_path": target,
                    "status": state,
                    **_source_position(item),
                }
            )
            if state == "resolved" and target:
                local_targets.add(target)
            if state == "external":
                roots_with_external_imports.add(path)

    tasks: list[dict] = []
    workflows: list[dict] = []
    for path, document in sorted(documents.items()):
        content = files[path]
        for task in document.tasks:
            source = _slice_source(content, task)
            tasks.append(
                {
                    "id": f"{path}::{task.name}",
                    "name": task.name,
                    "file_path": path,
                    "source_digest": digest(source),
                    **_source_position(task),
                    "inputs": [_declaration_payload(item) for item in (task.inputs or [])],
                    "outputs": [_declaration_payload(item) for item in (task.outputs or [])],
                    "runtime_keys": sorted((task.runtime or {}).keys()),
                }
            )
        if document.workflow is not None:
            workflows.append(
                {
                    "name": document.workflow.name,
                    "file_path": path,
                    **_source_position(document.workflow),
                }
            )

    diagnostics = [
        item
        for result in file_results.values()
        for item in result.get("diagnostics", [])
    ]
    diagnostics.extend(
        {
            "code": "WDL_IMPORT_MISSING",
            "stage": "wdl_analysis",
            "severity": "error",
            "message": f"Imported WDL is not present in the package: {item['uri']}.",
            "file_path": item["file_path"],
            **({"location": {"line": item.get("line"), "column": item.get("column")}} if item.get("line") else {}),
        }
        for item in imports
        if item["status"] == "missing"
    )
    root_modules = sorted(set(files) - local_targets) or sorted(files)
    for path in root_modules:
        if path in roots_with_external_imports:
            continue
        diagnostics.extend(_bundle_typecheck(files, path))
    deduplicated_diagnostics = []
    seen_diagnostics = set()
    for item in diagnostics:
        key = (
            item.get("code"),
            item.get("file_path"),
            item.get("location", {}).get("line"),
            item.get("message"),
        )
        if key not in seen_diagnostics:
            seen_diagnostics.add(key)
            deduplicated_diagnostics.append(item)

    versions = sorted(
        {item.get("wdl_version") for item in file_results.values() if item.get("wdl_version")}
    )
    return {
        "status": "invalid" if deduplicated_diagnostics else "valid",
        "parsed": all(item.get("parsed") for item in file_results.values()),
        "wdl_version": versions[0] if len(versions) == 1 else None,
        "wdl_versions": versions,
        "package": {
            "file_count": len(files),
            "module_count": len(root_modules),
            "resolved_import_count": sum(item["status"] == "resolved" for item in imports),
            "missing_import_count": sum(item["status"] == "missing" for item in imports),
            "external_import_count": sum(item["status"] == "external" for item in imports),
        },
        "summary": {
            "task_count": len(tasks),
            "workflow_count": len(workflows),
            "import_count": len(imports),
            "error_count": len(deduplicated_diagnostics),
        },
        "files": [file_results[path] for path in sorted(file_results)],
        "imports": imports,
        "tasks": tasks,
        "workflows": workflows,
        "diagnostics": deduplicated_diagnostics,
    }


def build_wdl_archive(
    files: dict[str, str],
    entrypoint: str,
    *,
    asset: dict | None = None,
) -> bytes:
    output = io.BytesIO()
    manifest = {
        "mainWorkflowURL": entrypoint,
        "files": [
            {"path": path, "digest": digest(content)}
            for path, content in sorted(files.items())
        ],
        **({"bioworkflow": asset} if asset else {}),
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        entries = {
            "MANIFEST.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            **files,
        }
        for path, content in sorted(entries.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))
    return output.getvalue()


def build_wdl_library_archive(files: dict[str, str], *, package: dict) -> bytes:
    output = io.BytesIO()
    manifest = {
        "schemaVersion": "1.0",
        "package": package,
        "files": [
            {"path": path, "digest": digest(content)}
            for path, content in sorted(files.items())
        ],
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        entries = {
            "WDL_PACKAGE.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            **files,
        }
        for path, content in sorted(entries.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))
    return output.getvalue()

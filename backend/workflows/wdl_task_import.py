from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from typing import Any

import WDL
from django.db import IntegrityError, transaction

from compiler_core import canonical_digest, validate_tool_spec

from .models import (
    ToolDocument,
    WDLAsset,
    WDLSourceFile,
    WDLSourceRevision,
    WDLToolPackageVersion,
)
from .wdl_packages import digest


SUPPORTED_TYPES = {
    "File",
    "Directory",
    "String",
    "Int",
    "Float",
    "Boolean",
    "Array[File]",
    "Array[String]",
    "Array[Int]",
    "Array[Float]",
    "Array[Boolean]",
    "Pair[File,File]",
}


class WDLTaskImportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedWDLTaskSource:
    path: str
    content: str


def _identifier(value: str, fallback: str = "legacy_task") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    if normalized[:1].isdigit():
        normalized = "_" + normalized
    return (normalized or fallback)[:128]


def _repository_name(asset: WDLAsset) -> str:
    repository = asset.source_repository.rstrip("/").removesuffix(".git")
    return repository.rsplit("/", 1)[-1] if repository else asset.slug


def recommended_tool_id(asset: WDLAsset, file_path: str, task_name: str) -> str:
    source = file_path.removesuffix(".wdl").replace("/", "_")
    return _identifier(f"{_repository_name(asset)}_{source}_{task_name}".lower())


def recommended_package_tool_id(
    package_version: WDLToolPackageVersion,
    file_path: str,
    task_name: str,
) -> str:
    source = file_path.removesuffix(".wdl").replace("/", "_")
    return _identifier(
        f"{package_version.package.slug}_{source}_{task_name}".lower()
    )


def _literal_value(expression) -> Any:
    if expression is None:
        raise ValueError
    if hasattr(expression, "value"):
        return expression.value
    if type(expression).__name__ == "String":
        rendered = str(expression)
        try:
            return ast.literal_eval(rendered)
        except (SyntaxError, ValueError):
            raise ValueError from None
    raise ValueError


def _semantic_type(wdl_type: str) -> str:
    normalized = wdl_type.removesuffix("?")
    return {
        "File": "core.file.any",
        "Directory": "core.directory",
        "String": "core.string",
        "Int": "core.integer",
        "Float": "core.float",
        "Boolean": "core.boolean",
        "Array[File]": "core.array.file",
        "Array[String]": "core.array.string",
        "Array[Int]": "core.array.integer",
        "Array[Float]": "core.array.float",
        "Array[Boolean]": "core.array.boolean",
        "Pair[File,File]": "core.pair.file",
    }.get(normalized, "core.string")


def _port_type(raw_type: str, warnings: list[str]) -> tuple[str, bool]:
    optional = raw_type.endswith("?")
    normalized = raw_type[:-1] if optional else raw_type
    if normalized not in SUPPORTED_TYPES:
        warnings.append(f"原 WDL 类型 {raw_type} 暂不受 ToolSpec 支持，已按 String 导入。")
        normalized = "String"
    return normalized, optional


def _input_port(declaration, warnings: list[str]) -> dict:
    raw_type = str(declaration.type)
    wdl_type, optional = _port_type(raw_type, warnings)
    payload = {
        "name": declaration.name,
        "label": declaration.name,
        "wdl_type": wdl_type,
        "semantic_type": _semantic_type(wdl_type),
        "required": not optional and declaration.expr is None,
    }
    if declaration.expr is not None:
        try:
            payload["default"] = _literal_value(declaration.expr)
        except ValueError:
            warnings.append(f"输入 {declaration.name} 的默认表达式需要人工确认。")
    return payload


def _output_port(declaration, warnings: list[str]) -> dict:
    raw_type = str(declaration.type)
    wdl_type, optional = _port_type(raw_type, warnings)
    return {
        "name": declaration.name,
        "label": declaration.name,
        "wdl_type": wdl_type,
        "semantic_type": _semantic_type(wdl_type),
        "optional": optional,
        "capture": {
            "mode": "expression",
            "value": str(declaration.expr),
        },
    }


def _command_template(task) -> str:
    command = "".join(item if isinstance(item, str) else str(item) for item in task.command.parts)
    return textwrap.dedent(command).strip("\n") + "\n"


def _container_image(task, warnings: list[str]) -> str:
    docker_input = next((item for item in task.inputs if item.name == "docker"), None)
    if docker_input is not None and docker_input.expr is not None:
        try:
            image = str(_literal_value(docker_input.expr)).strip()
            if image:
                if image.endswith(":latest"):
                    warnings.append("Docker 镜像使用 latest 标签，发布工具前建议固定版本或摘要。")
                return image
        except ValueError:
            pass
    warnings.append("未能从 WDL runtime 解析固定 Docker 镜像，已使用待确认占位镜像。")
    return "ubuntu:24.04"


def _tool_version(image: str) -> str:
    without_digest = image.split("@", 1)[0]
    tail = without_digest.rsplit("/", 1)[-1]
    tag = tail.rsplit(":", 1)[1] if ":" in tail else ""
    return tag.removeprefix("v")[:128] or "legacy-1.0"


def _runtime(task) -> dict:
    declarations = {item.name: item for item in task.inputs}
    result: dict[str, Any] = {}
    cpu = declarations.get("cpu")
    if cpu and cpu.expr is not None:
        try:
            result["cpu"] = max(1, int(_literal_value(cpu.expr)))
        except (ValueError, TypeError):
            pass
    for source_name, target_name in (("memory", "memory_gb"), ("disk", "disk_gb")):
        declaration = declarations.get(source_name)
        if not declaration or declaration.expr is None:
            continue
        try:
            value = str(_literal_value(declaration.expr))
        except ValueError:
            continue
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:G|GB)", value, re.IGNORECASE)
        if match:
            parsed = float(match.group(1))
            result[target_name] = int(parsed) if target_name == "disk_gb" else parsed
    return result


def _tool_spec_from_task(
    *,
    task,
    tool_id: str,
    description: str,
    source_repository: str,
    actor: str,
    source_wdl: dict,
    tags: list[str],
) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    image = _container_image(task, warnings)
    unsupported_runtime = sorted(
        set((task.runtime or {}).keys()) - {"docker", "cpu", "memory", "disk"}
    )
    if unsupported_runtime:
        warnings.append(
            "以下 runtime 配置已保留在来源信息中，发布前需要人工确认："
            + ", ".join(unsupported_runtime)
            + "。"
        )
    tool_spec = {
        "schema_version": "1.0.0",
        "id": tool_id,
        "name": task.name,
        "display_name": task.name,
        "tool_version": _tool_version(image),
        "description": description,
        "category": "historical_wdl",
        "container": {"engine": "docker", "image": image},
        "inputs": [_input_port(item, warnings) for item in (task.inputs or [])],
        "outputs": [_output_port(item, warnings) for item in (task.outputs or [])],
        "command": {
            "shell": "bash",
            "strict_mode": False,
            "template": _command_template(task),
        },
        "runtime": _runtime(task),
        "metadata": {
            "source_repository": source_repository,
            "created_by": actor,
            "tags": tags,
            "source_wdl": source_wdl,
            "migration_warnings": warnings,
        },
    }
    return tool_spec, warnings


def build_tool_draft(
    *,
    asset: WDLAsset,
    revision: WDLSourceRevision,
    source_file: WDLSourceFile | ResolvedWDLTaskSource,
    task_name: str,
    actor: str,
    tool_id: str = "",
) -> tuple[dict, list[str]]:
    try:
        document = WDL.parse_document(source_file.content, uri=source_file.path)
    except Exception as error:
        raise WDLTaskImportError(
            "WDL_TASK_SOURCE_INVALID",
            str(error).strip() or "The WDL task source cannot be parsed.",
        ) from error
    task = next((item for item in document.tasks if item.name == task_name), None)
    if task is None:
        raise WDLTaskImportError(
            "WDL_TASK_NOT_FOUND",
            f"Task {task_name} was not found in {source_file.path}.",
        )

    resolved_tool_id = _identifier(tool_id) if tool_id else recommended_tool_id(
        asset, source_file.path, task.name
    )
    return _tool_spec_from_task(
        task=task,
        tool_id=resolved_tool_id,
        description=f"从 {asset.name} 的 {source_file.path} / task {task.name} 导入。",
        source_repository=asset.source_repository,
        actor=actor,
        tags=["historical-wdl"],
        source_wdl={
            "asset_slug": asset.slug,
            "revision": revision.version,
            "file_path": source_file.path,
            "task_name": task.name,
            "source_digest": digest(source_file.content),
            "repository_revision": asset.source_revision,
        },
    )


def build_package_task_draft(
    *,
    package_version: WDLToolPackageVersion,
    source_file,
    task_name: str,
    actor: str,
) -> tuple[dict, list[str]]:
    try:
        document = WDL.parse_document(source_file.content, uri=source_file.path)
    except Exception as error:
        raise WDLTaskImportError(
            "WDL_TASK_SOURCE_INVALID",
            str(error).strip() or "The WDL task source cannot be parsed.",
        ) from error
    task = next((item for item in document.tasks if item.name == task_name), None)
    if task is None:
        raise WDLTaskImportError(
            "WDL_TASK_NOT_FOUND",
            f"Task {task_name} was not found in {source_file.path}.",
        )
    return _tool_spec_from_task(
        task=task,
        tool_id=recommended_package_tool_id(
            package_version, source_file.path, task.name
        ),
        description=(
            f"从工具包 {package_version.package.name}@{package_version.version} 的 "
            f"{source_file.path} / task {task.name} 拆解。"
        ),
        source_repository=package_version.source_repository,
        actor=actor,
        tags=["wdl-package", package_version.package.slug],
        source_wdl={
            "package_slug": package_version.package.slug,
            "package_version": package_version.version,
            "file_path": source_file.path,
            "task_name": task.name,
            "source_digest": digest(source_file.content),
            "repository_revision": package_version.source_revision,
        },
    )


def import_package_task_as_tool_draft(
    *,
    package_version: WDLToolPackageVersion,
    source_file,
    task_name: str,
    actor: str,
    replace: bool = False,
    base_draft_version: int | None = None,
    base_draft_digest: str = "",
) -> tuple[ToolDocument, bool, list[str]]:
    tool_spec, warnings = build_package_task_draft(
        package_version=package_version,
        source_file=source_file,
        task_name=task_name,
        actor=actor,
    )
    def same_source(existing: ToolDocument) -> bool:
        source = existing.draft_spec.get("metadata", {}).get("source_wdl", {})
        incoming = tool_spec["metadata"]["source_wdl"]
        return all(
            source.get(key) == incoming.get(key)
            for key in ("package_slug", "package_version", "file_path", "task_name", "source_digest")
        )

    return _persist_imported_tool_draft(
        tool_spec=tool_spec,
        warnings=warnings,
        replace=replace,
        base_draft_version=base_draft_version,
        base_draft_digest=base_draft_digest,
        same_source=same_source,
        exists_message=(
            f"Tool draft {tool_spec['id']} already exists; review it before replacing the source."
        ),
    )


def import_task_as_tool_draft(
    *,
    asset: WDLAsset,
    revision: WDLSourceRevision,
    source_file: WDLSourceFile | ResolvedWDLTaskSource,
    task_name: str,
    actor: str,
    tool_id: str = "",
    replace: bool = False,
    base_draft_version: int | None = None,
    base_draft_digest: str = "",
) -> tuple[ToolDocument, bool, list[str]]:
    tool_spec, warnings = build_tool_draft(
        asset=asset,
        revision=revision,
        source_file=source_file,
        task_name=task_name,
        actor=actor,
        tool_id=tool_id,
    )
    def same_source(existing: ToolDocument) -> bool:
        source = existing.draft_spec.get("metadata", {}).get("source_wdl", {})
        return (
            source.get("file_path") == source_file.path
            and source.get("task_name") == task_name
            and source.get("source_digest") == digest(source_file.content)
        )

    return _persist_imported_tool_draft(
        tool_spec=tool_spec,
        warnings=warnings,
        replace=replace,
        base_draft_version=base_draft_version,
        base_draft_digest=base_draft_digest,
        same_source=same_source,
        exists_message=(
            f"Tool draft {tool_spec['id']} already exists; choose another tool ID."
        ),
    )


def _persist_imported_tool_draft(
    *,
    tool_spec: dict,
    warnings: list[str],
    replace: bool,
    base_draft_version: int | None,
    base_draft_digest: str,
    same_source,
    exists_message: str,
) -> tuple[ToolDocument, bool, list[str]]:
    validation = validate_tool_spec(tool_spec)

    def update_existing(document: ToolDocument):
        if not replace:
            if same_source(document):
                source = document.draft_spec.get("metadata", {}).get("source_wdl", {})
                return document, False, source.get("migration_warnings", [])
            raise WDLTaskImportError("TOOL_DRAFT_EXISTS", exists_message)
        if base_draft_version is None or not base_draft_digest:
            raise WDLTaskImportError(
                "TOOL_DRAFT_PRECONDITION_REQUIRED",
                "Replacing a tool draft requires its current version and digest.",
            )
        current_digest = canonical_digest(document.draft_spec)
        if (
            base_draft_version != document.draft_version
            or base_draft_digest != current_digest
        ):
            raise WDLTaskImportError(
                "TOOL_DRAFT_CONFLICT",
                "Tool draft changed after it was loaded. Reload before replacing it.",
            )
        document.draft_spec = tool_spec
        document.validation = validation
        document.draft_version += 1
        document.save(
            update_fields=[
                "draft_spec",
                "validation",
                "draft_version",
                "updated_at",
            ]
        )
        return document, False, warnings

    with transaction.atomic():
        existing = (
            ToolDocument.objects.select_for_update()
            .filter(tool_id=tool_spec["id"])
            .first()
        )
        if existing is not None:
            return update_existing(existing)
        try:
            with transaction.atomic():
                document = ToolDocument.objects.create(
                    tool_id=tool_spec["id"],
                    draft_spec=tool_spec,
                    validation=validation,
                )
        except IntegrityError:
            existing = ToolDocument.objects.select_for_update().get(
                tool_id=tool_spec["id"]
            )
            return update_existing(existing)
        return document, True, warnings

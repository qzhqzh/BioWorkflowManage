from __future__ import annotations

import base64
import binascii
import re
from pathlib import PurePosixPath
from typing import Any


MINIWDL = "miniwdl"
NEXTFLOW = "nextflow"
NEXTFLOW_ENGINE_VERSION = "25.04.8"
SUPPORTED_EXECUTION_ENGINES = (MINIWDL, NEXTFLOW)

MAX_SOURCE_FILES = 512
MAX_SOURCE_BYTES = 25 * 1024 * 1024
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PINNED_IMAGE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$"
)
MANAGED_NEXTFLOW_PARAMS = {"fastqList", "database", "results", "task_name"}


class ExecutionSnapshotError(ValueError):
    pass


def normalize_source_path(value: Any) -> str:
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
        raise ExecutionSnapshotError(f"执行包路径无效：{candidate or '<empty>'}。")
    normalized = "/".join(parts)
    if len(normalized) > 512:
        raise ExecutionSnapshotError(f"执行包路径过长：{normalized}。")
    return normalized


def normalize_relative_directory(value: Any) -> str:
    candidate = str(value or ".").strip().replace("\\", "/")
    if candidate in {"", "."}:
        return "."
    return normalize_source_path(candidate)


def decode_bundle_file(value: Any, *, path: str) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    if not isinstance(value, dict) or value.get("encoding") != "base64":
        raise ExecutionSnapshotError(f"执行包文件编码无效：{path}。")
    content = value.get("content")
    if not isinstance(content, str):
        raise ExecutionSnapshotError(f"执行包文件内容无效：{path}。")
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ExecutionSnapshotError(f"执行包文件 Base64 无效：{path}。") from error


def _validate_bundle_files(bundle: dict[str, Any]) -> tuple[dict[str, Any], str]:
    files = bundle.get("files")
    entrypoint = normalize_source_path(bundle.get("entrypoint"))
    if not isinstance(files, dict) or not files or len(files) > MAX_SOURCE_FILES:
        raise ExecutionSnapshotError(
            f"执行包必须包含 1 到 {MAX_SOURCE_FILES} 个文件。"
        )
    normalized_files: dict[str, Any] = {}
    folded_paths: set[str] = set()
    total_bytes = 0
    for raw_path, value in files.items():
        path = normalize_source_path(raw_path)
        folded = path.casefold()
        if folded in folded_paths:
            raise ExecutionSnapshotError(f"执行包存在重复路径：{path}。")
        total_bytes += len(decode_bundle_file(value, path=path))
        if total_bytes > MAX_SOURCE_BYTES:
            raise ExecutionSnapshotError(
                f"执行包解码后超过 {MAX_SOURCE_BYTES} 字节。"
            )
        normalized_files[path] = value
        folded_paths.add(folded)
    if entrypoint not in normalized_files:
        raise ExecutionSnapshotError(f"执行入口不在固定包中：{entrypoint}。")
    executable_files = bundle.get("executable_files") or []
    if not isinstance(executable_files, list):
        raise ExecutionSnapshotError("执行包 executable_files 必须是数组。")
    normalized_executables = [normalize_source_path(path) for path in executable_files]
    if len(set(normalized_executables)) != len(normalized_executables) or any(
        path not in normalized_files for path in normalized_executables
    ):
        raise ExecutionSnapshotError("执行包 executable_files 包含重复或不存在的路径。")
    return normalized_files, entrypoint


def _validate_nextflow_manifest(
    manifest: Any,
    *,
    output_names: set[str] | None,
) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ExecutionSnapshotError("Nextflow runtime_manifest.schema_version 必须为 1。")
    unknown_manifest_keys = set(manifest) - {
        "schema_version",
        "engine_version",
        "profile",
        "input_adapter",
        "fixed_params",
        "path_params",
        "database_relative_path",
        "container_images",
        "outputs",
    }
    if unknown_manifest_keys:
        raise ExecutionSnapshotError("Nextflow runtime_manifest 包含未知字段。")
    engine_version = str(manifest.get("engine_version") or "")
    if (
        not VERSION_PATTERN.fullmatch(engine_version)
        or engine_version != NEXTFLOW_ENGINE_VERSION
    ):
        raise ExecutionSnapshotError(
            f"首期 Nextflow engine_version 必须为 {NEXTFLOW_ENGINE_VERSION}。"
        )
    profile = str(manifest.get("profile") or "")
    if not PROFILE_PATTERN.fullmatch(profile):
        raise ExecutionSnapshotError("Nextflow profile 格式无效。")

    adapter = manifest.get("input_adapter")
    if not isinstance(adapter, dict) or adapter.get("kind") != "paired_fastq_csv":
        raise ExecutionSnapshotError("首期 Nextflow 只支持 paired_fastq_csv 输入适配器。")
    for key in ("read1", "read2"):
        if not IDENTIFIER_PATTERN.fullmatch(str(adapter.get(key) or "")):
            raise ExecutionSnapshotError(f"Nextflow input_adapter.{key} 格式无效。")

    fixed_params = manifest.get("fixed_params")
    if not isinstance(fixed_params, dict):
        raise ExecutionSnapshotError("Nextflow fixed_params 必须是 object。")
    for key, value in fixed_params.items():
        if not IDENTIFIER_PATTERN.fullmatch(str(key)) or not isinstance(
            value, (str, int, float, bool)
        ):
            raise ExecutionSnapshotError(f"Nextflow 固定参数无效：{key}。")
        if key in MANAGED_NEXTFLOW_PARAMS:
            raise ExecutionSnapshotError(f"Nextflow 固定参数不得覆盖受管参数：{key}。")

    path_params = manifest.get("path_params") or []
    if not isinstance(path_params, list):
        raise ExecutionSnapshotError("Nextflow path_params 必须是数组。")
    path_param_names: set[str] = set()
    for index, item in enumerate(path_params):
        if not isinstance(item, dict) or set(item) != {
            "name",
            "root",
            "relative_path",
            "kind",
        }:
            raise ExecutionSnapshotError(
                f"Nextflow path_params[{index}] 字段无效。"
            )
        name = str(item.get("name") or "")
        root = str(item.get("root") or "")
        kind = str(item.get("kind") or "")
        relative_path = normalize_source_path(item.get("relative_path"))
        if (
            not IDENTIFIER_PATTERN.fullmatch(name)
            or name in MANAGED_NEXTFLOW_PARAMS
            or name in fixed_params
            or name in path_param_names
            or root not in {"source", "database"}
            or kind not in {"file", "directory"}
            or relative_path != str(item.get("relative_path") or "")
        ):
            raise ExecutionSnapshotError(
                f"Nextflow path_params[{index}] 定义无效。"
            )
        path_param_names.add(name)

    database_relative_path = normalize_relative_directory(
        manifest.get("database_relative_path")
    )
    if database_relative_path != str(manifest.get("database_relative_path") or "."):
        raise ExecutionSnapshotError("Nextflow database_relative_path 未规范化。")

    images = manifest.get("container_images")
    if not isinstance(images, dict) or not images.get("default"):
        raise ExecutionSnapshotError("Nextflow 必须声明固定 digest 的默认容器镜像。")
    if set(images) - {"default", "labels", "processes", "container_user_processes"}:
        raise ExecutionSnapshotError("Nextflow container_images 包含未知字段。")
    labels = images.get("labels") or {}
    processes = images.get("processes") or {}
    container_user_processes = images.get("container_user_processes") or []
    if not isinstance(labels, dict) or any(
        not PROFILE_PATTERN.fullmatch(str(label)) for label in labels
    ):
        raise ExecutionSnapshotError("Nextflow 容器 label 映射无效。")
    if not isinstance(processes, dict) or not processes or any(
        not PROFILE_PATTERN.fullmatch(str(process)) for process in processes
    ):
        raise ExecutionSnapshotError("Nextflow 必须声明固定 digest 的 process 容器映射。")
    if (
        not isinstance(container_user_processes, list)
        or len(set(container_user_processes)) != len(container_user_processes)
        or any(process not in processes for process in container_user_processes)
    ):
        raise ExecutionSnapshotError("Nextflow 容器用户 process 映射无效。")
    pinned_images = [images.get("default"), *labels.values(), *processes.values()]
    if any(not PINNED_IMAGE_PATTERN.fullmatch(str(image)) for image in pinned_images):
        raise ExecutionSnapshotError("Nextflow 容器镜像必须使用 repo@sha256 固定。")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ExecutionSnapshotError("Nextflow 必须声明输出采集规则。")
    names: set[str] = set()
    for index, item in enumerate(outputs):
        if not isinstance(item, dict):
            raise ExecutionSnapshotError(f"Nextflow outputs[{index}] 必须是 object。")
        name = str(item.get("name") or "")
        pattern = str(item.get("glob") or "").replace("\\", "/")
        if not IDENTIFIER_PATTERN.fullmatch(name) or name in names:
            raise ExecutionSnapshotError(f"Nextflow 输出名称无效或重复：{name}。")
        path = PurePosixPath(pattern)
        if (
            not pattern
            or path.is_absolute()
            or ".." in path.parts
            or "\x00" in pattern
        ):
            raise ExecutionSnapshotError(f"Nextflow 输出 glob 无效：{pattern}。")
        names.add(name)
    if output_names is not None and names != output_names:
        raise ExecutionSnapshotError("Nextflow 输出采集规则与接口契约不一致。")


def validate_execution_snapshot(
    execution_engine: str,
    bundle: Any,
    runtime_manifest: Any,
    *,
    output_names: set[str] | None = None,
) -> tuple[dict[str, Any], str]:
    if execution_engine not in SUPPORTED_EXECUTION_ENGINES:
        raise ExecutionSnapshotError(f"不支持的执行引擎：{execution_engine}。")
    if not isinstance(bundle, dict):
        raise ExecutionSnapshotError("固定执行包必须是 object。")
    files, entrypoint = _validate_bundle_files(bundle)
    if execution_engine == MINIWDL:
        if entrypoint != "workflow.wdl" or not entrypoint.endswith(".wdl"):
            raise ExecutionSnapshotError("MiniWDL 编译结果缺少 workflow.wdl。")
        if runtime_manifest not in ({}, None):
            raise ExecutionSnapshotError("MiniWDL 当前不接受额外 runtime_manifest。")
        execution = bundle.get("execution")
        if execution not in (None, {}):
            expected = {"engine": MINIWDL, "runtime_manifest": {}}
            if execution != expected:
                raise ExecutionSnapshotError("MiniWDL 执行快照与固定包不一致。")
        return files, entrypoint

    if not entrypoint.endswith(".nf"):
        raise ExecutionSnapshotError("Nextflow 执行入口必须是 .nf 文件。")
    _validate_nextflow_manifest(runtime_manifest, output_names=output_names)
    process_count = len(runtime_manifest["container_images"]["processes"])
    if (
        isinstance(bundle.get("call_count"), bool)
        or bundle.get("call_count") != process_count
    ):
        raise ExecutionSnapshotError(
            "Nextflow call_count 必须与固定 process 映射数量一致。"
        )
    expected_execution = {
        "engine": NEXTFLOW,
        "runtime_manifest": runtime_manifest,
    }
    if bundle.get("execution") != expected_execution:
        raise ExecutionSnapshotError("Nextflow runtime_manifest 未绑定到固定包摘要。")
    return files, entrypoint

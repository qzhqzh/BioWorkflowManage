from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

from .analysis_runtime import (
    AnalysisRunLeaseLost,
    _LeaseHeartbeat,
    _event,
    _materialize_source,
    _terminate_process_group,
    _update_run,
    _verify_run_resource_manifests,
    _write_json,
)
from .integration_outputs import ResourceSnapshotBudget, build_output_manifest
from .models import AnalysisRun
from .object_inputs import stage_run_object_inputs, verify_run_object_inputs


NEXTFLOW_VERSION_PATTERN = re.compile(r"\bversion\s+([0-9]+\.[0-9]+\.[0-9]+)\b")
SAFE_TASK_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
SAFE_EXECUTION_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_./:@+=-]+$")


def _groovy_string(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise RuntimeError("Nextflow 配置值包含非法控制字符。")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _nextflow_environment(run_directory: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "JAVA_HOME",
            "LANG",
            "LC_ALL",
            "DOCKER_HOST",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY",
        )
        if os.environ.get(key)
    }
    runtime_home = run_directory / ".nextflow-runtime"
    nextflow_home = runtime_home / "home"
    temporary_directory = runtime_home / "tmp"
    nextflow_home.mkdir(parents=True, mode=0o700)
    temporary_directory.mkdir(mode=0o700)
    environment.update(
        {
            "HOME": str(runtime_home),
            "TMPDIR": str(temporary_directory),
            "NXF_HOME": str(nextflow_home),
            "NXF_ANSI_LOG": "false",
            "NXF_OFFLINE": "true",
        }
    )
    return environment


def _verify_nextflow_version(
    executable: str,
    expected_version: str,
    *,
    environment: dict[str, str],
) -> None:
    if shutil.which("java") is None:
        raise RuntimeError("nextflow worker 中没有 Java 可执行文件。")
    try:
        completed = subprocess.run(
            [executable, "-version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("无法确认 nextflow 运行时版本。") from error
    output = f"{completed.stdout}\n{completed.stderr}"
    match = NEXTFLOW_VERSION_PATTERN.search(output)
    if completed.returncode != 0 or match is None:
        raise RuntimeError("nextflow 运行时版本检查失败。")
    if match.group(1) != expected_version:
        raise RuntimeError(
            f"nextflow 运行时版本不匹配：需要 {expected_version}，"
            f"实际 {match.group(1)}。"
        )


def _run_execution_directory(run_directory: Path) -> Path:
    local_root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    execution_root = Path(settings.ANALYSIS_RUN_EXECUTION_ROOT).resolve()
    try:
        relative = run_directory.resolve().relative_to(local_root)
    except ValueError as error:
        raise RuntimeError("Nextflow 运行目录不在受管根目录内。") from error
    execution_directory = execution_root / relative
    if execution_directory != run_directory.resolve():
        raise RuntimeError(
            "Nextflow host worker 要求 ANALYSIS_RUN_ROOT 与 "
            "ANALYSIS_RUN_EXECUTION_ROOT 指向同一绝对路径。"
        )
    return execution_directory


def _managed_database_path(relative_path: str) -> Path:
    local_root = Path(settings.ANALYSIS_DATABASE_ROOT).resolve()
    execution_root = Path(settings.ANALYSIS_DATABASE_EXECUTION_ROOT).resolve()
    local_path = (local_root / relative_path).resolve()
    try:
        relative = local_path.relative_to(local_root)
    except ValueError as error:
        raise RuntimeError("Nextflow 数据库路径逃逸受管根目录。") from error
    if not local_path.is_dir():
        raise RuntimeError("Nextflow 固定数据库资源尚未就绪。")
    execution_path = execution_root / relative
    if execution_path != local_path:
        raise RuntimeError(
            "Nextflow host worker 要求 ANALYSIS_DATABASE_ROOT 与 "
            "ANALYSIS_DATABASE_EXECUTION_ROOT 使用相同绝对路径。"
        )
    return execution_path


def _input_value(run: AnalysisRun, port_name: str) -> str:
    key = f"{run.workflow_name}.{port_name}"
    value = run.input_values.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Nextflow 运行输入缺少 {port_name}。")
    if not SAFE_EXECUTION_PATH_PATTERN.fullmatch(value):
        raise RuntimeError(f"Nextflow 运行输入路径包含不支持的字符：{port_name}。")
    if not Path(value).is_file():
        raise RuntimeError(f"Nextflow 运行输入文件不存在：{port_name}。")
    return value


def _write_fastq_list(
    run: AnalysisRun,
    path: Path,
    adapter: dict[str, Any],
) -> None:
    sample_id = SAFE_TASK_NAME_PATTERN.sub("_", run.sample_id).strip("._-")
    if not sample_id:
        raise RuntimeError("Nextflow 样本编号无法转换为安全任务名。")
    read1 = _input_value(run, str(adapter["read1"]))
    read2 = _input_value(run, str(adapter["read2"]))
    path.write_text(f"{sample_id},{read1},{read2}\n", encoding="utf-8")


def _write_nextflow_config(
    path: Path,
    *,
    run_id: str,
    run_execution_directory: Path,
    manifest: dict[str, Any],
) -> None:
    images = manifest["container_images"]
    lines = [
        "process.executor = 'local'",
        f"process.container = {_groovy_string(str(images['default']))}",
        "process.containerOptions = ''",
        "docker.enabled = true",
        "docker.remove = true",
        f"docker.temp = {_groovy_string(str(run_execution_directory / 'docker-tmp'))}",
        "docker.runOptions = "
        + _groovy_string(f"--label bioworkflow.analysis_run_id={run_id}"),
    ]
    for label, image in sorted((images.get("labels") or {}).items()):
        lines.extend(
            [
                "process {",
                f"  withLabel: {_groovy_string(str(label))} {{",
                f"    container = {_groovy_string(str(image))}",
                "  }",
                "}",
            ]
        )
    for process, image in sorted((images.get("processes") or {}).items()):
        container_options = (
            f"-u {os.getuid()}:{os.getgid()}"
            if process in (images.get("container_user_processes") or [])
            else ""
        )
        lines.extend(
            [
                "process {",
                f"  withName: {_groovy_string(str(process))} {{",
                "    executor = 'local'",
                f"    container = {_groovy_string(str(image))}",
                f"    containerOptions = {_groovy_string(container_options)}",
                "  }",
                "}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _path_parameter_arguments(
    manifest: dict[str, Any],
    *,
    source_directory: Path,
    database_path: Path,
) -> list[str]:
    arguments: list[str] = []
    roots = {
        "source": source_directory.resolve(),
        "database": database_path.resolve(),
    }
    for item in manifest.get("path_params") or []:
        root = roots[str(item["root"])]
        relative = Path(str(item["relative_path"]))
        candidate = root / relative
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(
                    f"Nextflow 固定路径参数包含符号链接：{item['name']}。"
                )
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise RuntimeError(
                f"Nextflow 固定路径参数逃逸受管根目录：{item['name']}。"
            ) from error
        expected = resolved.is_file() if item["kind"] == "file" else resolved.is_dir()
        if not expected:
            raise RuntimeError(f"Nextflow 固定路径参数尚未就绪：{item['name']}。")
        if not SAFE_EXECUTION_PATH_PATTERN.fullmatch(str(resolved)):
            raise RuntimeError(
                f"Nextflow 固定路径参数包含不支持的字符：{item['name']}。"
            )
        arguments.extend([f"--{item['name']}", str(resolved)])
    return arguments


def _nextflow_arguments(
    executable: str,
    *,
    entrypoint: Path,
    config_path: Path,
    fastq_list: Path,
    source_directory: Path,
    database_path: Path,
    attempt_directory: Path,
    manifest: dict[str, Any],
    task_name: str,
) -> list[str]:
    results = attempt_directory / "results"
    work = attempt_directory / "work"
    arguments = [
        executable,
        "-C",
        str(config_path),
        "run",
        str(entrypoint),
        "-work-dir",
        str(work),
        "-with-trace",
        str(attempt_directory / "trace.tsv"),
        "-with-timeline",
        str(attempt_directory / "timeline.html"),
        "-with-report",
        str(attempt_directory / "report.html"),
        "-ansi-log",
        "false",
        "--fastqList",
        str(fastq_list),
        "--database",
        str(database_path),
        "--results",
        str(results),
        "--task_name",
        task_name,
    ]
    for key, value in sorted(manifest["fixed_params"].items()):
        arguments.extend(
            [
                f"--{key}",
                str(value).lower() if isinstance(value, bool) else str(value),
            ]
        )
    arguments.extend(
        _path_parameter_arguments(
            manifest,
            source_directory=source_directory,
            database_path=database_path,
        )
    )
    return arguments


def _execution_output_path(path: Path) -> str:
    local_root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    execution_root = Path(settings.ANALYSIS_RUN_EXECUTION_ROOT).resolve()
    try:
        relative = path.resolve().relative_to(local_root)
    except ValueError as error:
        raise RuntimeError("Nextflow 输出不在受管运行目录内。") from error
    return str(execution_root / relative)


def _collect_outputs(
    run: AnalysisRun,
    results_directory: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    results_root = results_directory.resolve()
    contract = {
        str(item.get("name") or ""): str(item.get("wdl_type") or "String")
        for item in run.request_payload.get("integration_output_contract") or []
        if isinstance(item, dict)
    }
    outputs: dict[str, Any] = {}
    for item in manifest["outputs"]:
        name = str(item["name"])
        matches = []
        for candidate in sorted(results_directory.glob(str(item["glob"]))):
            try:
                candidate.resolve().relative_to(results_root)
            except ValueError as error:
                raise RuntimeError(f"Nextflow 输出路径逃逸：{name}。") from error
            if candidate.is_file() or candidate.is_dir():
                matches.append(candidate)
        if not matches and bool(item.get("required", True)):
            raise RuntimeError(f"Nextflow 必需输出缺失：{name}。")
        if len(matches) > 1 and not contract.get(name, "").startswith("Array["):
            raise RuntimeError(f"Nextflow 输出匹配到多个文件：{name}。")
        value: Any
        if contract.get(name, "").startswith("Array["):
            value = [_execution_output_path(match) for match in matches]
        else:
            value = _execution_output_path(matches[0]) if matches else None
        outputs[f"{run.workflow_name}.{name}"] = value
    return {"outputs": outputs}


def cleanup_nextflow_containers_for_run(
    run_directory: Path,
    run_id: str,
    *,
    docker_client=None,
) -> tuple[list[str], list[str]]:
    root = run_directory.resolve()
    created_client = docker_client is None
    removed: list[str] = []
    errors: list[str] = []
    try:
        if docker_client is None:
            import docker

            docker_client = docker.from_env(timeout=10)
        containers = docker_client.containers.list(
            all=True,
            filters={"label": f"bioworkflow.analysis_run_id={run_id}"},
        )
        for container in containers:
            mounts = container.attrs.get("Mounts") or []
            belongs_to_run = False
            for mount in mounts:
                source = str(mount.get("Source") or "")
                if not source or not Path(source).is_absolute():
                    continue
                try:
                    Path(source).resolve().relative_to(root)
                    belongs_to_run = True
                    break
                except (OSError, ValueError):
                    continue
            if not belongs_to_run:
                continue
            name = str(getattr(container, "name", None) or container.id)
            try:
                if container.status == "running":
                    container.stop(timeout=5)
                container.remove(force=True)
                removed.append(name)
            except Exception as error:
                errors.append(f"{name}: {error}")
    except Exception as error:
        errors.append(str(error))
    finally:
        if created_client and docker_client is not None:
            try:
                docker_client.close()
            except Exception:
                pass
    return removed, errors


def execute_nextflow_analysis_run(
    run: AnalysisRun,
    heartbeat: _LeaseHeartbeat | None = None,
) -> None:
    executable = shutil.which("nextflow")
    if executable is None:
        raise RuntimeError("analysis-worker 中没有 nextflow 可执行文件。")
    if shutil.which("docker") is None:
        raise RuntimeError("analysis-worker 中没有 Docker CLI。")
    root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_directory = root / str(run.id)
    manifest = run.runtime_manifest

    def resource_checkpoint() -> None:
        if heartbeat is not None and heartbeat.lease_lost.is_set():
            raise AnalysisRunLeaseLost(
                f"analysis run {run.id} lease was lost during resource verification"
            )

    snapshot_budget = ResourceSnapshotBudget(
        deadline_seconds=settings.ANALYSIS_WORKER_RESOURCE_MANIFEST_TIMEOUT_SECONDS,
        checkpoint=resource_checkpoint,
    )
    _verify_run_resource_manifests(
        run,
        checkpoint=resource_checkpoint,
        snapshot_budget=snapshot_budget,
    )
    staged_object_count = stage_run_object_inputs(run, checkpoint=resource_checkpoint)
    if staged_object_count:
        _verify_run_resource_manifests(
            run,
            checkpoint=resource_checkpoint,
            snapshot_budget=ResourceSnapshotBudget(
                deadline_seconds=(
                    settings.ANALYSIS_WORKER_RESOURCE_MANIFEST_TIMEOUT_SECONDS
                ),
                checkpoint=resource_checkpoint,
            ),
        )
        _event(
            run,
            f"已校验并固定 {staged_object_count} 个对象存储输入。",
            kind="input",
            details={"object_count": staged_object_count},
        )

    _update_run(run, work_directory=str(run_directory))
    run_directory.mkdir(parents=False, exist_ok=False)
    environment = _nextflow_environment(run_directory)
    _verify_nextflow_version(
        executable,
        str(manifest["engine_version"]),
        environment=environment,
    )
    run_execution_directory = _run_execution_directory(run_directory)
    entrypoint = _materialize_source(run, run_directory)
    attempt_directory = run_directory / "attempt-1"
    attempt_directory.mkdir()
    (attempt_directory / "work").mkdir()
    (attempt_directory / "results").mkdir()
    (run_execution_directory / "docker-tmp").mkdir()
    fastq_list = run_directory / "fastq-list.csv"
    _write_fastq_list(run, fastq_list, manifest["input_adapter"])
    config_path = attempt_directory / "nextflow.config"
    _write_nextflow_config(
        config_path,
        run_id=str(run.id),
        run_execution_directory=run_execution_directory,
        manifest=manifest,
    )
    database_path = _managed_database_path(
        str(manifest.get("database_relative_path") or ".")
    )
    task_name = SAFE_TASK_NAME_PATTERN.sub("_", run.sample_id).strip("._-")
    _write_json(run_directory / "inputs.json", run.input_values)
    _write_json(
        run_directory / "request.json",
        {
            "run_id": str(run.id),
            "execution_engine": run.execution_engine,
            "engine_version": manifest["engine_version"],
            "runtime_profile": manifest["profile"],
            "source_digest": run.source_digest,
            "workflow_name": run.workflow_name,
            "sample_id": run.sample_id,
            "created_at": run.created_at,
        },
    )
    if staged_object_count:
        verify_run_object_inputs(run, checkpoint=resource_checkpoint)

    arguments = _nextflow_arguments(
        executable,
        entrypoint=entrypoint,
        config_path=config_path,
        fastq_list=fastq_list,
        source_directory=entrypoint.parent,
        database_path=database_path,
        attempt_directory=attempt_directory,
        manifest=manifest,
        task_name=task_name,
    )
    _update_run(
        run,
        status=AnalysisRun.Status.RUNNING,
        progress=12,
        current_step="Nextflow 正在启动 LC103 AMP 流程",
    )
    _event(
        run,
        "已固定受信任执行包与运行参数，启动 Nextflow。",
        kind="nextflow",
        details={
            "engine_version": manifest["engine_version"],
            "profile": manifest["profile"],
        },
    )
    log_path = run_directory / "nextflow.log"
    significant_events = 0
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            arguments,
            cwd=entrypoint.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            env=environment,
        )
        if heartbeat is not None:
            heartbeat.watch(process)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log_handle.write(line)
                log_handle.flush()
                message = line.strip()
                if not message:
                    continue
                significant = any(
                    token in message.casefold()
                    for token in (
                        "submitted process",
                        "completed at",
                        "error",
                        "failed",
                        "warning",
                    )
                )
                if significant and significant_events < 500:
                    significant_events += 1
                    level = (
                        "error"
                        if any(token in message.casefold() for token in ("error", "failed"))
                        else "warning"
                        if "warning" in message.casefold()
                        else "info"
                    )
                    _event(
                        run,
                        message,
                        kind="nextflow",
                        level=level,
                    )
                    _update_run(
                        run,
                        progress=min(90, 12 + significant_events),
                        current_step=message[:256],
                    )
            exit_code = process.wait()
        finally:
            _terminate_process_group(process)
            if heartbeat is not None:
                heartbeat.unwatch(process)
            _, cleanup_errors = cleanup_nextflow_containers_for_run(
                run_directory,
                str(run.id),
            )
    if heartbeat is not None and heartbeat.lease_lost.is_set():
        raise AnalysisRunLeaseLost(f"analysis run {run.id} lease is no longer active")
    if cleanup_errors:
        raise RuntimeError(
            "Nextflow 任务容器清理失败：" + "; ".join(cleanup_errors)[:4000]
        )
    if exit_code != 0:
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
        except OSError:
            tail = ""
        raise RuntimeError(
            f"Nextflow 运行失败（exit {exit_code}）。"
            + (f"\n{tail}" if tail else "")
        )

    result = _collect_outputs(run, attempt_directory / "results", manifest)
    _write_json(attempt_directory / "result.json", result)

    def output_snapshot_checkpoint() -> None:
        if heartbeat is not None and heartbeat.lease_lost.is_set():
            raise AnalysisRunLeaseLost(f"analysis run {run.id} lease is no longer active")

    output_manifest, output_status, output_error = build_output_manifest(
        run,
        result,
        checkpoint=output_snapshot_checkpoint,
    )
    _update_run(
        run,
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        current_step=("分析完成" if output_error is None else "执行完成，但输出清单不完整"),
        outputs=result,
        output_manifest=output_manifest,
        output_status=output_status,
        error="" if output_error is None else "输出清单不完整。",
        error_code=output_error["code"] if output_error else "",
        error_category=output_error["category"] if output_error else "",
        error_retryable=output_error["retryable"] if output_error else False,
        error_details=output_error["details"] if output_error else {},
        finished_at=timezone.now(),
        lease_token=None,
        worker_heartbeat_at=None,
        lease_expires_at=None,
    )
    if output_error:
        _event(
            run,
            "Nextflow 执行成功，但输出清单不完整。",
            kind="output",
            level="error",
            details=output_error["details"],
        )
    else:
        _event(run, "Nextflow 分析完成，结果文件已就绪。")

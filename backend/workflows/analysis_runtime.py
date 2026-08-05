from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AnalysisRun, AnalysisRunEvent
from .wdl_packages import normalize_package_path
from .wdl_source_references import (
    effective_package_files,
    reference_specs_for_revision,
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _event(
    run: AnalysisRun,
    message: str,
    *,
    kind: str = "status",
    level: str = "info",
    details: dict[str, Any] | None = None,
) -> None:
    AnalysisRunEvent.objects.create(
        run=run,
        kind=kind,
        level=level,
        message=message[:1000],
        details=details or {},
    )


def _update_run(run: AnalysisRun, **values) -> None:
    for name, value in values.items():
        setattr(run, name, value)
    run.save(update_fields=[*values, "updated_at"])


def claim_next_run() -> AnalysisRun | None:
    with transaction.atomic():
        run = (
            AnalysisRun.objects.select_for_update(skip_locked=True)
            .select_related("asset", "revision")
            .filter(status=AnalysisRun.Status.QUEUED)
            .order_by("created_at")
            .first()
        )
        if run is None:
            return None
        run.status = AnalysisRun.Status.PREPARING
        run.progress = 5
        run.current_step = "正在准备 WDL 与输入"
        run.started_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "progress",
                "current_step",
                "started_at",
                "updated_at",
            ]
        )
        _event(run, "worker 已领取运行，开始准备输入。")
        return run


def _revision_bundle(run: AnalysisRun) -> tuple[dict[str, str], str]:
    revision = run.revision
    local_files = {item.path: item.content for item in revision.files.all()}
    if not local_files:
        local_files = {run.asset.source_filename: revision.content}
        entrypoint = run.asset.source_filename
    else:
        entrypoint = next(
            (item.path for item in revision.files.all() if item.is_entry),
            run.asset.source_filename,
        )
    files, _ = effective_package_files(
        local_files,
        reference_specs_for_revision(revision),
    )
    return files, normalize_package_path(entrypoint)


def _materialize_source(run: AnalysisRun, run_directory: Path) -> Path:
    files, entrypoint = _revision_bundle(run)
    source_directory = run_directory / "source"
    source_directory.mkdir(parents=True)
    for relative_path, content in files.items():
        normalized = normalize_package_path(relative_path)
        target = source_directory / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    entry = source_directory / entrypoint
    if not entry.is_file():
        raise RuntimeError(f"WDL 入口文件不存在：{entrypoint}")
    return entry


def _log_message(line: str) -> tuple[str, str, dict[str, Any]]:
    value = line.strip()
    if not value:
        return "", "info", {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value, "info", {}
    if not isinstance(payload, dict):
        return value, "info", {}
    message = str(
        payload.get("message")
        or payload.get("msg")
        or payload.get("event")
        or value
    )
    level = str(payload.get("level") or "info").lower()
    safe_details = {
        key: payload[key]
        for key in ("call", "job", "task", "status")
        if key in payload and isinstance(payload[key], (str, int, float, bool))
    }
    return message, level, safe_details


def _result_error(result_path: Path, fallback: str) -> str:
    if not result_path.is_file():
        return fallback
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("cause") or fallback)
        if error:
            return str(error)
    return fallback


def execute_analysis_run(run: AnalysisRun) -> None:
    executable = shutil.which("miniwdl")
    if executable is None:
        raise RuntimeError("analysis-worker 中没有 miniwdl 可执行文件。")
    root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_directory = root / str(run.id)
    run_directory.mkdir(parents=False, exist_ok=False)
    work_directory = run_directory / "work"
    work_directory.mkdir()
    result_path = run_directory / "result.json"
    input_path = run_directory / "inputs.json"
    request_path = run_directory / "request.json"
    stdout_path = run_directory / "miniwdl.stdout.log"
    stderr_path = run_directory / "miniwdl.log"

    entrypoint = _materialize_source(run, run_directory)
    _write_json(input_path, run.input_values)
    _write_json(
        request_path,
        {
            "run_id": str(run.id),
            "asset": run.asset.slug,
            "revision": run.revision.version,
            "digest": run.revision.digest,
            "workflow_name": run.workflow_name,
            "sample_id": run.sample_id,
            "created_at": run.created_at,
        },
    )
    _update_run(
        run,
        status=AnalysisRun.Status.RUNNING,
        progress=12,
        current_step="miniwdl 正在校验流程",
        work_directory=str(run_directory),
    )
    _event(run, f"已固定 {run.asset.name} v{run.revision.version}，启动 miniwdl。")

    arguments = [
        executable,
        "run",
        "--dir",
        f"{work_directory}{os.sep}.",
        "--error-json",
        "--no-cache",
        "--no-color",
        "--log-json",
        "--no-outside-imports",
        "--as-me",
        "-o",
        str(result_path),
        "-i",
        str(input_path),
        str(entrypoint),
    ]
    total_calls = 1
    workflows = run.revision.analysis.get("workflows", [])
    if workflows:
        total_calls = max(1, int(workflows[0].get("structure", {}).get("call_count", 1)))
    completed_calls: set[str] = set()
    event_count = 0
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            arguments,
            cwd=run_directory,
            stdout=stdout_handle,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stderr is not None
        for line in process.stderr:
            stderr_handle.write(line)
            stderr_handle.flush()
            message, level, details = _log_message(line)
            if not message:
                continue
            lowered = message.lower()
            call_name = str(details.get("call") or details.get("job") or details.get("task") or "")
            if call_name and re.search(r"\b(done|complete|succeeded)\b", lowered):
                completed_calls.add(call_name)
            progress = min(92, 15 + int(75 * len(completed_calls) / total_calls))
            significant = (
                level in {"warning", "error", "critical"}
                or call_name
                or any(word in lowered for word in ("workflow", "container", "task", "done", "fail"))
            )
            if significant and event_count < 500:
                event_count += 1
                _event(
                    run,
                    message,
                    kind="miniwdl",
                    level="error" if level in {"error", "critical"} else level,
                    details=details,
                )
                _update_run(
                    run,
                    progress=progress,
                    current_step=(call_name or message)[:256],
                )
        exit_code = process.wait()

    if exit_code != 0:
        message = _result_error(
            result_path,
            f"miniwdl 运行失败（exit {exit_code}），请查看运行日志。",
        )
        _update_run(
            run,
            status=AnalysisRun.Status.FAILED,
            progress=100,
            current_step="运行失败",
            error=message[:8000],
            finished_at=timezone.now(),
        )
        _event(run, message, level="error")
        return

    if not result_path.is_file():
        raise RuntimeError("miniwdl 已退出，但没有生成 result.json。")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("miniwdl result.json 不是 JSON object。")
    _update_run(
        run,
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        current_step="分析完成",
        outputs=result,
        error="",
        finished_at=timezone.now(),
    )
    _event(run, "分析完成，结果文件已就绪。")


def process_analysis_run(run: AnalysisRun) -> None:
    try:
        execute_analysis_run(run)
    except Exception as error:
        _update_run(
            run,
            status=AnalysisRun.Status.FAILED,
            progress=100,
            current_step="运行失败",
            error=str(error)[:8000],
            finished_at=timezone.now(),
        )
        _event(run, str(error), level="error")

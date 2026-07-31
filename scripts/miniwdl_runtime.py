#!/usr/bin/env python3
"""miniwdl validation and execution harness for BioWorkflowManage.

This module deliberately keeps orchestration outside Django. It can be used by
Docker Compose, CI, and tests without coupling workflow execution to the web
process.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_ROOT = PROJECT_ROOT / "examples" / "miniwdl-execution"
CASE_MANIFEST_ROOT = EXECUTION_ROOT / "cases"
SMOKE_WDL = EXECUTION_ROOT / "smoke" / "workflow.wdl"
CORE_STATIC_WDLS = (
    PROJECT_ROOT / "examples" / "phase1-fastp" / "expected" / "workflow.wdl",
    PROJECT_ROOT
    / "examples"
    / "phase1-fastp-bwa"
    / "expected"
    / "workflow.wdl",
    SMOKE_WDL,
)
SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
INPUT_FORMATS = {"fasta", "fastq_gz"}
SOURCE_EXECUTION_STATES = {
    "awaiting_data": "无已知结构阻塞，待真实数据验证",
    "blocked": "仅静态有效，尚不可运行",
    "verified": "已通过真实数据运行验证",
}


class RuntimeHarnessError(RuntimeError):
    """An actionable framework or preflight failure."""


@dataclass(frozen=True)
class InputFile:
    wdl_name: str
    relative_path: Path
    description: str
    input_format: str
    pair: str | None
    mate: int | None


@dataclass(frozen=True)
class ExpectedOutput:
    wdl_name: str
    output_type: str


@dataclass(frozen=True)
class CaseManifest:
    case_id: str
    title: str
    description: str
    wdl: Path
    runnable: bool
    inputs: tuple[InputFile, ...]
    expected_outputs: tuple[ExpectedOutput, ...]
    source_wdl: Path | None
    source_execution_status: str | None
    source_runtime_blockers: tuple[str, ...]


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def project_path(relative_path: str, label: str) -> Path:
    candidate = (PROJECT_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise RuntimeHarnessError(f"{label} 不能位于项目目录之外：{relative_path}") from error
    if not candidate.is_file():
        raise RuntimeHarnessError(f"{label} 不存在：{candidate}")
    return candidate


def validate_relative_data_path(value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts:
        raise RuntimeHarnessError(f"{label} 必须是相对路径：{value}")
    if any(part in {"", ".", ".."} or not SAFE_PATH_SEGMENT.fullmatch(part) for part in path.parts):
        raise RuntimeHarnessError(
            f"{label} 只能包含安全路径片段 [A-Za-z0-9._-]：{value}"
        )
    return path


def load_case(case_id: str) -> CaseManifest:
    if not SAFE_PATH_SEGMENT.fullmatch(case_id):
        raise RuntimeHarnessError(f"非法案例标识：{case_id}")
    manifest_path = CASE_MANIFEST_ROOT / case_id / "case.json"
    if not manifest_path.is_file():
        available = ", ".join(list_cases()) or "无"
        raise RuntimeHarnessError(f"未知案例 {case_id}；可用案例：{available}")

    document = read_json(manifest_path)
    if document.get("schema_version") != 1:
        raise RuntimeHarnessError(f"{manifest_path} 的 schema_version 必须为 1")
    if document.get("id") != case_id:
        raise RuntimeHarnessError(f"{manifest_path} 的 id 必须为 {case_id}")

    raw_inputs = document.get("inputs")
    if not isinstance(raw_inputs, dict) or not raw_inputs:
        raise RuntimeHarnessError(f"{manifest_path} 必须声明非空 inputs")
    inputs: list[InputFile] = []
    for wdl_name, raw_input in sorted(raw_inputs.items()):
        if not isinstance(wdl_name, str) or not isinstance(raw_input, dict):
            raise RuntimeHarnessError(f"{manifest_path} 的 inputs 格式错误")
        input_format = raw_input.get("format")
        if input_format not in INPUT_FORMATS:
            allowed = ", ".join(sorted(INPUT_FORMATS))
            raise RuntimeHarnessError(
                f"{manifest_path} 的 input format 必须是：{allowed}"
            )
        pair = raw_input.get("pair")
        mate = raw_input.get("mate")
        if (pair is None) != (mate is None):
            raise RuntimeHarnessError(
                f"{manifest_path} 的 pair 和 mate 必须同时提供或同时省略"
            )
        if pair is not None and (
            not isinstance(pair, str)
            or not SAFE_PATH_SEGMENT.fullmatch(pair)
            or mate not in {1, 2}
        ):
            raise RuntimeHarnessError(
                f"{manifest_path} 包含非法 FASTQ pair/mate 声明"
            )
        if pair is not None and input_format != "fastq_gz":
            raise RuntimeHarnessError(
                f"{manifest_path} 只有 fastq_gz 可以声明 pair/mate"
            )
        inputs.append(
            InputFile(
                wdl_name=wdl_name,
                relative_path=validate_relative_data_path(
                    str(raw_input.get("path", "")),
                    f"{case_id}.{wdl_name}",
                ),
                description=str(raw_input.get("description", "")),
                input_format=input_format,
                pair=pair,
                mate=mate,
            )
        )
    pairs: dict[str, list[int]] = {}
    for input_file in inputs:
        if input_file.pair is not None:
            assert input_file.mate is not None
            pairs.setdefault(input_file.pair, []).append(input_file.mate)
    if any(sorted(mates) != [1, 2] for mates in pairs.values()):
        raise RuntimeHarnessError(
            f"{manifest_path} 的每个 paired FASTQ 必须各声明 mate 1 和 mate 2"
        )

    raw_outputs = document.get("expected_outputs", [])
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise RuntimeHarnessError(
            f"{manifest_path} 的 expected_outputs 必须是非空数组"
        )
    expected_outputs: list[ExpectedOutput] = []
    for raw_output in raw_outputs:
        if (
            not isinstance(raw_output, dict)
            or not isinstance(raw_output.get("name"), str)
            or raw_output.get("type") not in {"File", "String"}
        ):
            raise RuntimeHarnessError(f"{manifest_path} 包含非法 expected_outputs")
        expected_outputs.append(
            ExpectedOutput(
                wdl_name=raw_output["name"],
                output_type=raw_output["type"],
            )
        )

    source = document.get("compiled_source")
    source_wdl: Path | None = None
    source_execution_status: str | None = None
    source_runtime_blockers: tuple[str, ...] = ()
    if source is not None:
        if not isinstance(source, dict):
            raise RuntimeHarnessError(f"{manifest_path} 的 compiled_source 格式错误")
        source_wdl = project_path(str(source.get("wdl", "")), "编译产物 WDL")
        source_execution_status = source.get("execution_status")
        if source_execution_status not in SOURCE_EXECUTION_STATES:
            allowed = ", ".join(sorted(SOURCE_EXECUTION_STATES))
            raise RuntimeHarnessError(
                f"{manifest_path} 的 execution_status 必须是：{allowed}"
            )
        blockers = source.get("runtime_blockers", [])
        if not isinstance(blockers, list) or not all(
            isinstance(item, str) and item for item in blockers
        ):
            raise RuntimeHarnessError(f"{manifest_path} 的 runtime_blockers 格式错误")
        source_runtime_blockers = tuple(blockers)
        if source_execution_status == "blocked" and not source_runtime_blockers:
            raise RuntimeHarnessError(
                f"{manifest_path} 的 blocked 状态必须说明 runtime_blockers"
            )

    runnable = document.get("runnable")
    if not isinstance(runnable, bool):
        raise RuntimeHarnessError(f"{manifest_path} 的 runnable 必须是 JSON boolean")
    return CaseManifest(
        case_id=case_id,
        title=str(document.get("title", case_id)),
        description=str(document.get("description", "")),
        wdl=project_path(str(document.get("wdl", "")), "运行案例 WDL"),
        runnable=runnable,
        inputs=tuple(inputs),
        expected_outputs=tuple(expected_outputs),
        source_wdl=source_wdl,
        source_execution_status=source_execution_status,
        source_runtime_blockers=source_runtime_blockers,
    )


def list_cases() -> list[str]:
    if not CASE_MANIFEST_ROOT.is_dir():
        return []
    return sorted(
        path.parent.name for path in CASE_MANIFEST_ROOT.glob("*/case.json")
    )


def static_wdls() -> tuple[Path, ...]:
    paths = list(CORE_STATIC_WDLS)
    for case_id in list_cases():
        manifest = load_case(case_id)
        paths.append(manifest.wdl)
        if manifest.source_wdl is not None:
            paths.append(manifest.source_wdl)
    return tuple(dict.fromkeys(path.resolve() for path in paths))


def runtime_root() -> Path:
    value = os.environ.get("MINIWDL_RUN_ROOT")
    if not value:
        raise RuntimeHarnessError("缺少 MINIWDL_RUN_ROOT 环境变量")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeHarnessError("MINIWDL_RUN_ROOT 必须是绝对路径")
    path = path.resolve()
    for name in ("cache", "cases", "home", "runs", "tmp"):
        (path / name).mkdir(parents=True, exist_ok=True)
    probe = path / f".write-probe-{uuid4().hex}"
    try:
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise RuntimeHarnessError(f"运行目录不可写：{path}: {error}") from error
    return path


def case_input_directory(root: Path, manifest: CaseManifest) -> Path:
    directory = (root / "cases" / manifest.case_id / "inputs").resolve()
    expected_parent = (root / "cases").resolve()
    try:
        directory.relative_to(expected_parent)
    except ValueError as error:
        raise RuntimeHarnessError("案例输入目录越过了运行根目录") from error
    return directory


def resolve_case_inputs(
    root: Path, manifest: CaseManifest
) -> tuple[dict[str, str], list[Path]]:
    input_directory = case_input_directory(root, manifest)
    resolved: dict[str, str] = {}
    missing: list[Path] = []
    for input_file in manifest.inputs:
        path = (input_directory / input_file.relative_path).resolve()
        try:
            path.relative_to(input_directory)
        except ValueError as error:
            raise RuntimeHarnessError(
                f"输入路径越过案例目录：{input_file.relative_path}"
            ) from error
        resolved[input_file.wdl_name] = str(path)
        if not path.is_file():
            missing.append(path)
    return resolved, missing


def miniwdl_command() -> str:
    executable = shutil.which("miniwdl")
    if executable is None:
        raise RuntimeHarnessError("当前环境没有 miniwdl 可执行文件")
    return executable


def run_process(arguments: list[str]) -> int:
    display = " ".join(arguments)
    print(f"\n$ {display}", flush=True)
    completed = subprocess.run(arguments, cwd=PROJECT_ROOT, check=False)
    return completed.returncode


def check_wdl(path: Path) -> None:
    if not path.is_file():
        raise RuntimeHarnessError(f"WDL 不存在：{path}")
    relative = path.relative_to(PROJECT_ROOT)
    print(f"\n[check] {relative}", flush=True)
    code = run_process(
        [
            miniwdl_command(),
            "check",
            "--no-outside-imports",
            str(path),
        ]
    )
    if code:
        raise RuntimeHarnessError(f"miniwdl 静态校验失败：{relative}")


def command_check() -> None:
    version = subprocess.run(
        [miniwdl_command(), "--version"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"miniwdl: {version}")
    paths = static_wdls()
    for path in paths:
        check_wdl(path)
    print(f"\n静态校验通过：{len(paths)} 个 WDL。")


def input_layout(manifest: CaseManifest, root: Path) -> dict[str, Any]:
    resolved, _ = resolve_case_inputs(root, manifest)
    return {
        "case": manifest.case_id,
        "title": manifest.title,
        "input_directory": str(case_input_directory(root, manifest)),
        "inputs": {
            input_file.wdl_name: {
                "path": resolved[input_file.wdl_name],
                "description": input_file.description,
                "format": input_file.input_format,
                "pair": input_file.pair,
                "mate": input_file.mate,
            }
            for input_file in manifest.inputs
        },
    }


def prepare_case(manifest: CaseManifest, root: Path) -> None:
    directory = case_input_directory(root, manifest)
    directory.mkdir(parents=True, exist_ok=True)
    layout_path = directory.parent / "expected-inputs.json"
    write_json(layout_path, input_layout(manifest, root))
    print(f"\n[{manifest.case_id}] {manifest.title}")
    print(f"输入目录：{directory}")
    for input_file in manifest.inputs:
        print(f"  - {input_file.relative_path}: {input_file.description}")
    print(f"布局说明：{layout_path}")


def fastq_record(path: Path) -> str:
    try:
        with gzip.open(path, mode="rt", encoding="ascii", newline="") as handle:
            lines = [handle.readline().rstrip("\r\n") for _ in range(4)]
    except (EOFError, OSError, UnicodeError) as error:
        raise RuntimeHarnessError(f"{path.name} 不是可读的 gzip FASTQ：{error}") from error
    header, sequence, plus, quality = lines
    if not header.startswith("@"):
        raise RuntimeHarnessError(f"{path.name} 的首条 FASTQ header 必须以 @ 开头")
    if not sequence or any(character.isspace() for character in sequence):
        raise RuntimeHarnessError(f"{path.name} 的首条 FASTQ sequence 为空或包含空白")
    if not plus.startswith("+"):
        raise RuntimeHarnessError(f"{path.name} 的首条 FASTQ 第三行必须以 + 开头")
    if len(quality) != len(sequence):
        raise RuntimeHarnessError(
            f"{path.name} 的首条 FASTQ sequence/quality 长度不一致"
        )
    identifier = header[1:].split(maxsplit=1)[0]
    if identifier.endswith(("/1", "/2")):
        identifier = identifier[:-2]
    if not identifier:
        raise RuntimeHarnessError(f"{path.name} 的首条 FASTQ read ID 为空")
    return identifier


def validate_fasta(path: Path) -> None:
    try:
        with path.open(encoding="ascii") as handle:
            header = ""
            sequence = ""
            for line in handle:
                value = line.strip()
                if not value:
                    continue
                if not header:
                    header = value
                    continue
                sequence = value
                break
    except (OSError, UnicodeError) as error:
        raise RuntimeHarnessError(f"{path.name} 不是可读的 FASTA：{error}") from error
    if not header.startswith(">") or len(header) == 1:
        raise RuntimeHarnessError(f"{path.name} 的首个非空行必须是 FASTA header")
    if not sequence or sequence.startswith(">"):
        raise RuntimeHarnessError(f"{path.name} 的首条 FASTA sequence 为空")
    if any(character.isspace() for character in sequence):
        raise RuntimeHarnessError(f"{path.name} 的首条 FASTA sequence 包含空白")


def validate_case_input_contents(
    manifest: CaseManifest,
    resolved: dict[str, str],
) -> None:
    pair_identifiers: dict[str, dict[int, str]] = {}
    for input_file in manifest.inputs:
        path = Path(resolved[input_file.wdl_name])
        if path.stat().st_size == 0:
            raise RuntimeHarnessError(f"输入文件为空：{path}")
        if input_file.input_format == "fastq_gz":
            identifier = fastq_record(path)
            if input_file.pair is not None:
                assert input_file.mate is not None
                pair_identifiers.setdefault(input_file.pair, {})[
                    input_file.mate
                ] = identifier
        elif input_file.input_format == "fasta":
            validate_fasta(path)

    for pair, identifiers in sorted(pair_identifiers.items()):
        if identifiers[1] != identifiers[2]:
            raise RuntimeHarnessError(
                f"paired FASTQ {pair} 的首条 read ID 不一致："
                f"{identifiers[1]} != {identifiers[2]}"
            )


def command_prepare(case_id: str | None) -> None:
    root = runtime_root()
    case_ids = [case_id] if case_id else list_cases()
    if not case_ids:
        raise RuntimeHarnessError("没有可准备的运行案例")
    for item in case_ids:
        prepare_case(load_case(item), root)
    print("\n只创建了目录和说明文件，没有生成或覆盖测试数据。")


def preflight_case(
    manifest: CaseManifest,
    root: Path,
    *,
    check_syntax: bool = True,
) -> tuple[dict[str, str], list[Path]]:
    print(f"\n[{manifest.case_id}] {manifest.title}")
    print(f"运行 WDL：{manifest.wdl.relative_to(PROJECT_ROOT)}")
    if manifest.description:
        print(f"说明：{manifest.description}")
    if check_syntax:
        check_wdl(manifest.wdl)

    if manifest.source_wdl:
        if check_syntax and manifest.source_wdl != manifest.wdl:
            check_wdl(manifest.source_wdl)
        state = SOURCE_EXECUTION_STATES[manifest.source_execution_status]
        print(
            "编译产物："
            f"{manifest.source_wdl.relative_to(PROJECT_ROOT)}（{state}）"
        )
        for blocker in manifest.source_runtime_blockers:
            print(f"  - 已知阻塞：{blocker}")

    resolved, missing = resolve_case_inputs(root, manifest)
    print(f"数据目录：{case_input_directory(root, manifest)}")
    for name, path in resolved.items():
        state = "存在" if Path(path).is_file() else "缺失"
        print(f"  - {name}: {path} [{state}]")
    if not missing:
        validate_case_input_contents(manifest, resolved)
        print("输入内容：基础格式与 paired read ID 检查通过")

    if not manifest.runnable:
        raise RuntimeHarnessError("案例清单已标记为不可运行")
    return resolved, missing


def command_preflight(case_id: str) -> None:
    root = runtime_root()
    manifest = load_case(case_id)
    _, missing = preflight_case(manifest, root)
    if missing:
        joined = "\n".join(f"  - {path}" for path in missing)
        raise RuntimeHarnessError(
            "WDL 静态校验通过，但真实运行数据尚未就绪：\n" + joined
        )
    print("\n预检通过，可以开始真实运行。")


def create_run_directory(root: Path, name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "run"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / "runs" / f"{safe_name}-{timestamp}-{uuid4().hex[:8]}"
    directory.mkdir(parents=False, exist_ok=False)
    return directory


def write_run_status(
    run_directory: Path,
    *,
    status: str,
    exit_code: int,
    verification: str,
    error: str | None = None,
) -> None:
    write_json(
        run_directory / "status.json",
        {
            "status": status,
            "exit_code": exit_code,
            "verification": verification,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "result": str(run_directory / "result.json"),
            "error": error,
        },
    )


def execute_wdl(
    *,
    root: Path,
    run_name: str,
    wdl: Path,
    inputs: dict[str, str] | None,
    metadata: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    run_directory = create_run_directory(root, run_name)
    result_path = run_directory / "result.json"
    request_path = run_directory / "request.json"
    input_path = run_directory / "resolved-inputs.json"
    write_json(
        request_path,
        {
            **metadata,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "wdl": str(wdl),
            "run_directory": str(run_directory),
        },
    )
    if inputs is not None:
        write_json(input_path, inputs)

    arguments = [
        miniwdl_command(),
        "run",
        "--dir",
        f"{run_directory}{os.sep}.",
        "--error-json",
        "--no-cache",
        "--no-color",
        "--no-outside-imports",
        "--as-me",
        "-o",
        str(result_path),
    ]
    if inputs is not None:
        arguments.extend(["-i", str(input_path)])
    arguments.append(str(wdl))

    code = run_process(arguments)
    write_run_status(
        run_directory,
        status="miniwdl_succeeded" if code == 0 else "failed",
        exit_code=code,
        verification="pending" if code == 0 else "not_run",
    )
    if code:
        raise RuntimeHarnessError(
            f"miniwdl 运行失败（exit {code}），诊断目录：{run_directory}"
        )
    if not result_path.is_file():
        write_run_status(
            run_directory,
            status="failed",
            exit_code=code,
            verification="failed",
            error="miniwdl 未生成结果 JSON",
        )
        raise RuntimeHarnessError(f"miniwdl 未生成结果 JSON：{result_path}")
    result = read_json(result_path)
    if not isinstance(result, dict):
        write_run_status(
            run_directory,
            status="failed",
            exit_code=code,
            verification="failed",
            error="miniwdl 结果不是 JSON object",
        )
        raise RuntimeHarnessError(f"miniwdl 结果格式错误：{result_path}")
    return run_directory, result


def output_mapping(result: dict[str, Any]) -> dict[str, Any]:
    outputs = result.get("outputs")
    if isinstance(outputs, dict):
        return outputs
    return result


def verify_expected_outputs(
    expected_outputs: tuple[ExpectedOutput, ...],
    result: dict[str, Any],
) -> None:
    outputs = output_mapping(result)
    for expected in expected_outputs:
        if expected.wdl_name not in outputs:
            raise RuntimeHarnessError(f"结果缺少输出：{expected.wdl_name}")
        value = outputs[expected.wdl_name]
        if expected.output_type == "File":
            if not isinstance(value, str) or not Path(value).is_file():
                raise RuntimeHarnessError(
                    f"文件输出不存在：{expected.wdl_name} -> {value}"
                )
            if Path(value).stat().st_size == 0:
                raise RuntimeHarnessError(
                    f"文件输出为空：{expected.wdl_name} -> {value}"
                )
        elif not isinstance(value, str):
            raise RuntimeHarnessError(f"字符串输出格式错误：{expected.wdl_name}")


def command_run(case_id: str) -> None:
    root = runtime_root()
    manifest = load_case(case_id)
    resolved, missing = preflight_case(manifest, root)
    if missing:
        joined = "\n".join(f"  - {path}" for path in missing)
        raise RuntimeHarnessError("缺少真实运行数据：\n" + joined)

    run_directory, result = execute_wdl(
        root=root,
        run_name=manifest.case_id,
        wdl=manifest.wdl,
        inputs=resolved,
        metadata={
            "kind": "case",
            "case": manifest.case_id,
            "title": manifest.title,
            "compiled_source_wdl": (
                str(manifest.source_wdl) if manifest.source_wdl else None
            ),
            "compiled_source_execution_status": manifest.source_execution_status,
            "compiled_source_runtime_blockers": list(
                manifest.source_runtime_blockers
            ),
        },
    )
    try:
        verify_expected_outputs(manifest.expected_outputs, result)
    except RuntimeHarnessError as error:
        write_run_status(
            run_directory,
            status="failed",
            exit_code=0,
            verification="failed",
            error=str(error),
        )
        raise
    write_run_status(
        run_directory,
        status="succeeded",
        exit_code=0,
        verification="passed",
    )
    print(f"\n真实案例运行通过：{manifest.case_id}")
    print(f"持久化结果：{run_directory}")


def command_smoke() -> None:
    root = runtime_root()
    check_wdl(SMOKE_WDL)
    run_directory, result = execute_wdl(
        root=root,
        run_name="container-smoke",
        wdl=SMOKE_WDL,
        inputs=None,
        metadata={"kind": "container-smoke"},
    )
    outputs = output_mapping(result)
    status = outputs.get("miniwdl_runtime_smoke.status")
    probe = outputs.get("miniwdl_runtime_smoke.probe")
    try:
        if status != "miniwdl-container-ok":
            raise RuntimeHarnessError(f"smoke 状态不符：{status!r}")
        if not isinstance(probe, str) or not Path(probe).is_file():
            raise RuntimeHarnessError(f"smoke 文件输出不存在：{probe}")
        if Path(probe).read_text(encoding="utf-8").strip() != status:
            raise RuntimeHarnessError("smoke 文件内容与状态输出不一致")
    except RuntimeHarnessError as error:
        write_run_status(
            run_directory,
            status="failed",
            exit_code=0,
            verification="failed",
            error=str(error),
        )
        raise
    write_run_status(
        run_directory,
        status="succeeded",
        exit_code=0,
        verification="passed",
    )
    print("\nminiwdl -> Docker -> task -> 持久化输出链路通过。")
    print(f"持久化结果：{run_directory}")


def command_doctor() -> None:
    root = runtime_root()
    docker_host = os.environ.get("DOCKER_HOST")
    if not docker_host:
        raise RuntimeHarnessError("缺少 DOCKER_HOST，未连接隔离执行引擎")
    try:
        import docker

        client = docker.from_env()
        info = client.info()
        client.close()
    except Exception as error:
        raise RuntimeHarnessError(f"无法访问 miniwdl Docker 引擎：{error}") from error

    swarm = info.get("Swarm") or {}
    version = subprocess.check_output(
        [miniwdl_command(), "--version"],
        text=True,
    ).strip()
    swarm_state = swarm.get("LocalNodeState", "unknown")
    print(f"miniwdl: {version}")
    print(f"DOCKER_HOST: {docker_host}")
    print(f"Docker ServerVersion: {info.get('ServerVersion', 'unknown')}")
    print(f"Docker StorageDriver: {info.get('Driver', 'unknown')}")
    print(f"Docker CPUs: {info.get('NCPU', 'unknown')}")
    print(f"Docker Memory: {info.get('MemTotal', 'unknown')}")
    print(f"Docker Swarm: {swarm_state}")
    print(f"持久化运行目录：{root}")
    if swarm_state == "active":
        print("基础连通检查通过，隔离 Swarm 已激活。")
    else:
        print("基础连通检查通过；首次 smoke/run 会初始化隔离单节点 Swarm。")
    print("doctor 不验证 task 和共享路径，smoke 才是完整执行门禁。")


def command_self_test() -> None:
    root = runtime_root()
    run_directory = create_run_directory(root, "miniwdl-self-test")
    code = run_process(
        [
            miniwdl_command(),
            "run_self_test",
            "--dir",
            str(run_directory),
            "--as-me",
        ]
    )
    write_json(
        run_directory / "harness-status.json",
        {
            "status": "succeeded" if code == 0 else "failed",
            "exit_code": code,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if code:
        raise RuntimeHarnessError(
            f"miniwdl 官方 self-test 失败，诊断目录：{run_directory}"
        )
    print(f"\nminiwdl 官方 self-test 通过：{run_directory}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="静态校验项目 WDL")
    subparsers.add_parser("doctor", help="检查隔离 Docker 执行引擎")
    subparsers.add_parser("smoke", help="执行无数据容器 smoke WDL")
    subparsers.add_parser(
        "self-test",
        help="执行 miniwdl 官方联网 self-test（诊断用途）",
    )

    prepare = subparsers.add_parser("prepare", help="创建案例数据目录")
    prepare.add_argument("case", nargs="?", choices=list_cases())

    preflight = subparsers.add_parser("preflight", help="校验案例和输入数据")
    preflight.add_argument("case", choices=list_cases())

    run = subparsers.add_parser("run", help="真实执行指定案例")
    run.add_argument("case", choices=list_cases())
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        if arguments.command == "check":
            command_check()
        elif arguments.command == "doctor":
            command_doctor()
        elif arguments.command == "smoke":
            command_smoke()
        elif arguments.command == "self-test":
            command_self_test()
        elif arguments.command == "prepare":
            command_prepare(arguments.case)
        elif arguments.command == "preflight":
            command_preflight(arguments.case)
        elif arguments.command == "run":
            command_run(arguments.case)
        else:  # pragma: no cover
            raise RuntimeHarnessError(f"不支持的命令：{arguments.command}")
    except RuntimeHarnessError as error:
        print(f"\n错误：{error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"\n错误：命令执行失败（exit {error.returncode}）", file=sys.stderr)
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

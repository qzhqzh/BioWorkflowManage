from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import AnalysisRun, AnalysisRunEvent, WDLAsset


FASTQ_PATTERN = re.compile(
    r"^(?P<prefix>.+?)(?P<marker>[_\.-]R)(?P<mate>[12])(?P<suffix>(?:[_\.-].*)?)\.(?P<extension>fastq|fq)\.gz$",
    re.IGNORECASE,
)
SAFE_SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_DISPLAY_VALUE = re.compile(r"^[\w\u4e00-\u9fff .()_-]{1,128}$", re.UNICODE)
MAX_DISCOVERED_FASTQ = 2000

WORKFLOW_PROFILES = {
    "solidtumorsingle": {
        "name": "实体瘤单样本",
        "workflow_name": "SolidTumorSingle",
        "mode": "single",
        "description": "一个肿瘤样本的 QC、比对、SNV、CNV、融合与结果汇总。",
    },
    "solidtumorpair": {
        "name": "实体瘤配对样本",
        "workflow_name": "SolidTumorPiar",
        "mode": "paired",
        "description": "肿瘤与对照样本配对分析，包含体细胞变异和双样本质控。",
    },
}


class AnalysisInputError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


def _error(code: str, message: str, http_status: int, *, details=None) -> Response:
    payload: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        payload["details"] = details
    return Response({"error": payload}, status=http_status)


def _actor(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_username()
    return "local-user"


def _safe_path(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise AnalysisInputError("ANALYSIS_PATH_INVALID", "资源路径不能为空。")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise AnalysisInputError(
            "ANALYSIS_PATH_INVALID",
            f"资源路径必须位于受管目录内：{relative_path}",
        )
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as error:
        raise AnalysisInputError(
            "ANALYSIS_PATH_INVALID",
            f"资源路径越过受管目录：{relative_path}",
        ) from error
    return candidate


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _sample_code(pair_stem: str) -> str:
    parts = [item for item in pair_stem.split("_") if item]
    if parts and re.fullmatch(r"L\d+", parts[-1], re.IGNORECASE):
        parts.pop()
    return (parts[-1] if parts else pair_stem)[-128:]


def discover_fastq_datasets() -> list[dict[str, Any]]:
    root = Path(settings.ANALYSIS_RAWDATA_ROOT)
    if not root.is_dir():
        return []
    pairs: dict[str, dict[int, Path]] = {}
    pair_stems: dict[str, str] = {}
    for index, path in enumerate(sorted(root.rglob("*"))):
        if index >= MAX_DISCOVERED_FASTQ:
            break
        if path.is_symlink() or not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) > 4:
            continue
        match = FASTQ_PATTERN.match(path.name)
        if not match:
            continue
        pair_name = (
            f"{match.group('prefix')}{match.group('marker')}{{R}}"
            f"{match.group('suffix')}.{match.group('extension').lower()}.gz"
        )
        key = str(relative.parent / pair_name)
        pairs.setdefault(key, {})[int(match.group("mate"))] = path
        pair_stems[key] = match.group("prefix")

    datasets: list[dict[str, Any]] = []
    for key, mates in sorted(pairs.items()):
        if set(mates) != {1, 2}:
            continue
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        files = []
        total_size = 0
        for mate in (1, 2):
            path = mates[mate]
            size = path.stat().st_size
            total_size += size
            files.append(
                {
                    "mate": mate,
                    "name": path.name,
                    "relative_path": path.relative_to(root).as_posix(),
                    "size": size,
                    "size_label": _format_size(size),
                }
            )
        sample_code = _sample_code(pair_stems[key])
        datasets.append(
            {
                "id": digest,
                "name": sample_code,
                "pair_key": key,
                "files": files,
                "total_size": total_size,
                "total_size_label": _format_size(total_size),
            }
        )
    return datasets


def _dataset_paths(dataset: dict[str, Any]) -> tuple[Path, Path]:
    root = Path(settings.ANALYSIS_RAWDATA_ROOT)
    paths = {
        item["mate"]: _safe_path(root, item["relative_path"])
        for item in dataset["files"]
    }
    return paths[1], paths[2]


def _first_fastq_identifier(path: Path) -> str:
    try:
        with gzip.open(path, mode="rt", encoding="ascii", newline="") as handle:
            lines = [handle.readline().rstrip("\r\n") for _ in range(4)]
    except (EOFError, OSError, UnicodeError) as error:
        raise AnalysisInputError(
            "ANALYSIS_FASTQ_INVALID",
            f"{path.name} 不是可读的 gzip FASTQ：{error}",
        ) from error
    header, sequence, plus, quality = lines
    if (
        not header.startswith("@")
        or not sequence
        or not plus.startswith("+")
        or len(sequence) != len(quality)
    ):
        raise AnalysisInputError(
            "ANALYSIS_FASTQ_INVALID",
            f"{path.name} 的首条 FASTQ 记录不完整。",
        )
    identifier = header[1:].split(maxsplit=1)[0]
    return identifier[:-2] if identifier.endswith(("/1", "/2")) else identifier


def _validate_dataset(dataset: dict[str, Any]) -> tuple[Path, Path]:
    read1, read2 = _dataset_paths(dataset)
    for path in (read1, read2):
        if not path.is_file() or path.stat().st_size == 0:
            raise AnalysisInputError(
                "ANALYSIS_FASTQ_MISSING",
                f"原始数据不存在或为空：{path.name}",
            )
    if _first_fastq_identifier(read1) != _first_fastq_identifier(read2):
        raise AnalysisInputError(
            "ANALYSIS_FASTQ_PAIR_MISMATCH",
            "R1 与 R2 的首条 read ID 不一致。",
        )
    return read1, read2


def load_database_catalog() -> dict[str, Any]:
    path = Path(settings.ANALYSIS_DATABASE_CATALOG)
    if not path.is_file():
        raise AnalysisInputError(
            "ANALYSIS_DATABASE_CATALOG_MISSING",
            "数据库 catalog.json 尚未就绪。",
            details={"path": "workspace/databases/catalog.json"},
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AnalysisInputError(
            "ANALYSIS_DATABASE_CATALOG_INVALID",
            f"数据库 catalog.json 无法读取：{error}",
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or not isinstance(document.get("references"), list)
        or not isinstance(document.get("panels"), list)
    ):
        raise AnalysisInputError(
            "ANALYSIS_DATABASE_CATALOG_INVALID",
            "数据库 catalog.json 必须符合 schema_version 1。",
        )
    return document


def _requirements(entry: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(settings.ANALYSIS_DATABASE_ROOT)
    results = []
    for item in entry.get("required", []):
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("path") or "")
        kind = str(item.get("kind") or "file")
        alternative_paths = item.get("alternatives", [])
        if not isinstance(alternative_paths, list):
            alternative_paths = []
        candidates = [relative_path, *(str(path) for path in alternative_paths)]
        present = False
        for candidate_path in candidates:
            try:
                candidate = _safe_path(root, candidate_path)
                present = candidate.is_dir() if kind == "directory" else candidate.is_file()
            except AnalysisInputError:
                present = False
            if present:
                break
        results.append(
            {
                "path": relative_path,
                "label": str(item.get("label") or relative_path),
                "kind": kind,
                "present": present,
            }
        )
    return results


def _catalog_entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
    requirements = _requirements(entry)
    return {
        key: value
        for key, value in entry.items()
        if key not in {"directories", "required", "bed", "gene_list", "tert_bed", "p1q19_bed", "druggable_region", "cnvkit_db"}
    } | {
        "ready": all(item["present"] for item in requirements),
        "requirements": requirements,
        "missing": [item for item in requirements if not item["present"]],
    }


def _workflow_payload(slug: str, profile: dict[str, str]) -> dict[str, Any]:
    asset = WDLAsset.objects.filter(slug=slug).first()
    revision = asset.source_revisions.first() if asset else None
    diagnostics = revision.analysis.get("diagnostics", []) if revision else []
    errors = [item for item in diagnostics if item.get("severity") == "error"]
    return {
        "slug": slug,
        **profile,
        "asset_name": asset.name if asset else "",
        "revision": revision.version if revision else None,
        "digest": revision.digest if revision else "",
        "ready": bool(asset and revision and not errors),
        "diagnostic_count": len(errors),
        "blockers": (
            [f"WDL 当前有 {len(errors)} 个静态错误，请先在工作台修复。"]
            if errors
            else ([] if asset else ["历史 WDL 资产尚未导入。"])
        ),
    }


def _find_by_id(entries: list[dict[str, Any]], entry_id: str, label: str) -> dict[str, Any]:
    entry = next((item for item in entries if item.get("id") == entry_id), None)
    if entry is None:
        raise AnalysisInputError(
            "ANALYSIS_SELECTION_INVALID",
            f"未找到所选{label}：{entry_id}",
        )
    return entry


def _resource(root: Path, relative_path: str, *, directory: bool = False) -> str:
    path = _safe_path(root, relative_path)
    present = path.is_dir() if directory else path.is_file()
    if not present:
        raise AnalysisInputError(
            "ANALYSIS_DATABASE_INCOMPLETE",
            f"数据库资源缺失：{relative_path}",
            details={"missing": [{"path": relative_path}]},
        )
    return str(path)


def _validate_safe_value(value: str, label: str, *, sample_id: bool = False) -> str:
    value = value.strip()
    pattern = SAFE_SAMPLE_ID if sample_id else SAFE_DISPLAY_VALUE
    if not pattern.fullmatch(value):
        raise AnalysisInputError(
            "ANALYSIS_VALUE_INVALID",
            f"{label}包含不安全字符或长度不符合要求。",
        )
    return value


def _build_inputs(
    profile: dict[str, str],
    dataset: dict[str, Any],
    control: dict[str, Any] | None,
    reference: dict[str, Any],
    panel: dict[str, Any],
    values: dict[str, str],
) -> dict[str, Any]:
    raw1, raw2 = _validate_dataset(dataset)
    control_paths = _validate_dataset(control) if control is not None else None
    database_root = Path(settings.ANALYSIS_DATABASE_ROOT)
    directories = reference.get("directories")
    if not isinstance(directories, dict):
        raise AnalysisInputError(
            "ANALYSIS_DATABASE_CATALOG_INVALID",
            "参考版本缺少 directories 映射。",
        )
    resolved_directories = {
        name: _resource(database_root, str(path), directory=True)
        for name, path in directories.items()
    }
    resolved_panel = {
        name: _resource(
            database_root,
            str(panel.get(name) or ""),
            directory=name == "cnvkit_db",
        )
        for name in (
            "bed",
            "gene_list",
            "tert_bed",
            "p1q19_bed",
            "druggable_region",
            "cnvkit_db",
        )
    }
    workflow_name = profile["workflow_name"]
    inputs: dict[str, Any] = {
        f"{workflow_name}.localdb": resolved_directories["localdb"],
        f"{workflow_name}.reference": resolved_directories["reference"],
        f"{workflow_name}.humandb": resolved_directories["humandb"],
        f"{workflow_name}.resource": resolved_directories["resource"],
        f"{workflow_name}.sample": values["sample_id"],
        f"{workflow_name}.sample_name": values["sample_name"],
        f"{workflow_name}.sample_type": values["sample_type"],
        f"{workflow_name}.sample_gender": values["sample_gender"],
        f"{workflow_name}.ref_version": str(reference["ref_version"]),
        f"{workflow_name}.fastq1": str(raw1),
        f"{workflow_name}.fastq2": str(raw2),
        f"{workflow_name}.bed": resolved_panel["bed"],
        f"{workflow_name}.gene_list": resolved_panel["gene_list"],
        f"{workflow_name}.tert_bed": resolved_panel["tert_bed"],
        f"{workflow_name}.p1q19_bed": resolved_panel["p1q19_bed"],
        "CallFusion.druggable_region": resolved_panel["druggable_region"],
    }
    if profile["mode"] == "single":
        inputs.update(
            {
                "AutoCNVKit_Panel.cnvdb": resolved_directories["cnvdb"],
                "AutoCNVKit_Panel.cnvkit_db": resolved_panel["cnvkit_db"],
                "Collect.database": resolved_directories["database"],
            }
        )
    else:
        if control_paths is None:
            raise AnalysisInputError(
                "ANALYSIS_CONTROL_REQUIRED",
                "配对流程需要选择一组对照样本 R1/R2。",
            )
        inputs.update(
            {
                f"{workflow_name}.database": resolved_directories["database"],
                f"{workflow_name}.cnvdb": resolved_directories["cnvdb"],
                f"{workflow_name}.cnvkit_db": resolved_panel["cnvkit_db"],
                f"{workflow_name}.fastq3": str(control_paths[0]),
                f"{workflow_name}.fastq4": str(control_paths[1]),
            }
        )
    return inputs


def _flatten_outputs(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        flattened = []
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten_outputs(item, name))
        return flattened
    if isinstance(value, list):
        flattened = []
        for index, item in enumerate(value):
            flattened.extend(_flatten_outputs(item, f"{prefix}[{index}]"))
        return flattened
    return [(prefix, value)]


def _output_payload(run: AnalysisRun) -> list[dict[str, Any]]:
    if not run.outputs or not run.work_directory:
        return []
    root = Path(run.work_directory).resolve()
    outputs = run.outputs.get("outputs", run.outputs)
    payload = []
    for key, value in _flatten_outputs(outputs):
        if isinstance(value, str):
            path = Path(value)
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                resolved = None
            if resolved is not None and resolved.is_file():
                payload.append(
                    {
                        "key": key,
                        "kind": "file",
                        "name": resolved.name,
                        "size": resolved.stat().st_size,
                        "size_label": _format_size(resolved.stat().st_size),
                        "download_url": (
                            f"/api/v1/analysis-runs/{run.id}/outputs"
                            f"?key={quote(key, safe='')}"
                        ),
                    }
                )
                continue
        payload.append({"key": key, "kind": "value", "value": value})
    return payload


def analysis_run_payload(run: AnalysisRun, *, include_events: bool = False) -> dict[str, Any]:
    payload = {
        "id": str(run.id),
        "workflow": {
            "slug": run.asset.slug,
            "name": run.asset.name,
            "workflow_name": run.workflow_name,
            "revision": run.revision.version,
            "digest": run.revision.digest,
        },
        "sample_id": run.sample_id,
        "sample_name": run.sample_name,
        "actor": run.actor,
        "status": run.status,
        "progress": run.progress,
        "current_step": run.current_step,
        "request": run.request_payload,
        "error": run.error,
        "outputs": _output_payload(run),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.updated_at,
    }
    if include_events:
        payload["events"] = [
            {
                "id": event.id,
                "kind": event.kind,
                "level": event.level,
                "message": event.message,
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in run.events.all()[:500]
        ]
    return payload


@api_view(["GET"])
def analysis_catalog(request):
    datasets = discover_fastq_datasets()
    workflows = [
        _workflow_payload(slug, profile)
        for slug, profile in WORKFLOW_PROFILES.items()
    ]
    try:
        catalog = load_database_catalog()
        references = [
            _catalog_entry_payload(item) for item in catalog["references"]
        ]
        panels = [_catalog_entry_payload(item) for item in catalog["panels"]]
        catalog_error = None
    except AnalysisInputError as error:
        references = []
        panels = []
        catalog_error = {"code": error.code, "message": str(error), "details": error.details}
        catalog = {"schema_version": 1}
    return Response(
        {
            "rawdata_directory": "workspace/rawdata",
            "database_directory": "workspace/databases",
            "datasets": datasets,
            "workflows": workflows,
            "database": {
                "schema_version": catalog.get("schema_version"),
                "references": references,
                "panels": panels,
                "error": catalog_error,
            },
        }
    )


@api_view(["GET", "POST"])
def analysis_runs(request):
    if request.method == "GET":
        runs = AnalysisRun.objects.select_related("asset", "revision")[:50]
        return Response({"results": [analysis_run_payload(run) for run in runs]})

    try:
        workflow_slug = str(request.data.get("workflow") or "")
        profile = WORKFLOW_PROFILES.get(workflow_slug)
        if profile is None:
            raise AnalysisInputError(
                "ANALYSIS_WORKFLOW_UNSUPPORTED",
                "当前运行页只允许已配置的受管 WDL。",
            )
        asset = WDLAsset.objects.filter(slug=workflow_slug).first()
        revision = asset.source_revisions.first() if asset else None
        if asset is None or revision is None:
            raise AnalysisInputError(
                "ANALYSIS_WORKFLOW_MISSING",
                "所选历史 WDL 尚未导入。",
            )
        errors = [
            item
            for item in revision.analysis.get("diagnostics", [])
            if item.get("severity") == "error"
        ]
        if errors:
            raise AnalysisInputError(
                "ANALYSIS_WORKFLOW_INVALID",
                f"所选 WDL revision v{revision.version} 有 {len(errors)} 个静态错误。",
                details={"diagnostic_count": len(errors)},
            )

        datasets = discover_fastq_datasets()
        dataset = _find_by_id(
            datasets,
            str(request.data.get("dataset") or ""),
            "原始数据",
        )
        control = None
        if profile["mode"] == "paired":
            control = _find_by_id(
                datasets,
                str(request.data.get("control_dataset") or ""),
                "对照数据",
            )
            if control["id"] == dataset["id"]:
                raise AnalysisInputError(
                    "ANALYSIS_CONTROL_INVALID",
                    "肿瘤样本和对照样本不能选择同一组 FASTQ。",
                )

        catalog = load_database_catalog()
        reference = _find_by_id(
            catalog["references"],
            str(request.data.get("reference") or ""),
            "参考版本",
        )
        panel = _find_by_id(
            catalog["panels"],
            str(request.data.get("panel") or ""),
            "Panel",
        )
        missing = [
            item
            for item in [*_requirements(reference), *_requirements(panel)]
            if not item["present"]
        ]
        if missing:
            raise AnalysisInputError(
                "ANALYSIS_DATABASE_INCOMPLETE",
                f"数据库资源尚缺 {len(missing)} 项。",
                details={"missing": missing},
            )

        sample_id = _validate_safe_value(
            str(request.data.get("sample_id") or dataset["name"]),
            "样本编号",
            sample_id=True,
        )
        sample_name = _validate_safe_value(
            str(request.data.get("sample_name") or sample_id),
            "样本名称",
        )
        sample_type = str(request.data.get("sample_type") or "tissue")
        if sample_type not in {"tissue", "blood", "plasma"}:
            raise AnalysisInputError("ANALYSIS_VALUE_INVALID", "样本类型不受支持。")
        gender_values = {"男": "男", "女": "女", "male": "男", "female": "女"}
        sample_gender = gender_values.get(
            str(request.data.get("sample_gender") or "女")
        )
        if sample_gender is None:
            raise AnalysisInputError("ANALYSIS_VALUE_INVALID", "请选择样本性别。")
        input_values = _build_inputs(
            profile,
            dataset,
            control,
            reference,
            panel,
            {
                "sample_id": sample_id,
                "sample_name": sample_name,
                "sample_type": sample_type,
                "sample_gender": sample_gender,
            },
        )
    except AnalysisInputError as error:
        return _error(error.code, str(error), status.HTTP_400_BAD_REQUEST, details=error.details)

    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name=profile["workflow_name"],
        sample_id=sample_id,
        sample_name=sample_name,
        actor=_actor(request),
        request_payload={
            "workflow": workflow_slug,
            "dataset": dataset["id"],
            "dataset_name": dataset["name"],
            "control_dataset": control["id"] if control else None,
            "control_dataset_name": control["name"] if control else None,
            "reference": reference["id"],
            "reference_name": reference.get("name", reference["id"]),
            "panel": panel["id"],
            "panel_name": panel.get("name", panel["id"]),
            "sample_type": sample_type,
            "sample_gender": sample_gender,
        },
        input_values=input_values,
    )
    AnalysisRunEvent.objects.create(run=run, message="运行已进入队列。")
    return Response(analysis_run_payload(run, include_events=True), status=status.HTTP_201_CREATED)


@api_view(["GET"])
def analysis_run_detail(request, run_id):
    run = get_object_or_404(
        AnalysisRun.objects.select_related("asset", "revision").prefetch_related("events"),
        pk=run_id,
    )
    return Response(analysis_run_payload(run, include_events=True))


@api_view(["GET"])
def analysis_run_output(request, run_id):
    run = get_object_or_404(AnalysisRun, pk=run_id)
    key = str(request.query_params.get("key") or "")
    output = next(
        (
            item
            for item in _output_payload(run)
            if item.get("kind") == "file" and item.get("key") == key
        ),
        None,
    )
    if output is None:
        return _error(
            "ANALYSIS_OUTPUT_NOT_FOUND",
            "输出文件不存在。",
            status.HTTP_404_NOT_FOUND,
        )
    values = dict(_flatten_outputs(run.outputs.get("outputs", run.outputs)))
    try:
        root = Path(run.work_directory).resolve()
        path = Path(values[key]).resolve()
        path.relative_to(root)
        if not path.is_file():
            raise ValueError
    except (OSError, TypeError, ValueError):
        return _error(
            "ANALYSIS_OUTPUT_NOT_FOUND",
            "输出文件不存在。",
            status.HTTP_404_NOT_FOUND,
        )
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=path.name,
        content_type=content_type,
    )

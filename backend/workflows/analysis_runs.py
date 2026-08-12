from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .annotation_tools import ANNOVAR_CANONICAL_IDS, ANNOVAR_RESOURCE_PATTERNS
from .models import (
    AnalysisRun,
    AnalysisRunEvent,
    WDLAsset,
    WorkflowDocument,
    WorkflowVersion,
)


FASTQ_PATTERN = re.compile(
    r"^(?P<prefix>.+?)(?P<marker>[_\.-]R)(?P<mate>[12])(?P<suffix>(?:[_\.-].*)?)\.(?P<extension>fastq|fq)\.gz$",
    re.IGNORECASE,
)
SAFE_SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_DISPLAY_VALUE = re.compile(r"^[\w\u4e00-\u9fff .()_-]{1,128}$", re.UNICODE)
MAX_DISCOVERED_FASTQ = 2000
PUBLISHED_WORKFLOW_PREFIX = "published:"
WDL_ASSET_WORKFLOW_PREFIX = "wdl-asset:"
SUPPORTED_PUBLISHED_INPUTS = {
    "bio.fastq.gz.r1",
    "bio.fastq.gz.r2",
    "bio.annotation.database_dir",
}

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
    "tumor-blood-single-production": {
        "name": "血液肿瘤单样本正式流程",
        "workflow_name": "TumorBloodSingle",
        "mode": "single",
        "description": "血液肿瘤单样本正式 WDL，包含完整本地依赖。",
        "required_reference": "hg38",
        "input_adapter": "pending",
    },
    "tumor-blood-pair-production": {
        "name": "血液肿瘤配对样本正式流程",
        "workflow_name": "TumorBloodPair",
        "mode": "paired",
        "description": "血液肿瘤肿瘤/对照配对正式 WDL，包含完整本地依赖。",
        "required_reference": "hg38",
        "input_adapter": "pending",
    },
}

BLOOD_TUMOR_HG38_REQUIREMENTS = (
    ("hg38/reference/Homo_sapiens_assembly38.fasta", "GRCh38 FASTA", "file"),
    ("hg38/reference/Homo_sapiens_assembly38.fasta.fai", "GRCh38 FASTA 索引", "file"),
    ("hg38/reference/Homo_sapiens_assembly38.dict", "GRCh38 sequence dictionary", "file"),
    ("hg38/reference/Homo_sapiens_assembly38.fasta.64.amb", "BWA amb 索引", "file"),
    ("hg38/reference/Homo_sapiens_assembly38.fasta.64.ann", "BWA ann 索引", "file"),
    ("hg38/reference/Homo_sapiens_assembly38.fasta.64.bwt", "BWA bwt 索引", "file"),
    ("hg38/reference/Homo_sapiens_assembly38.fasta.64.pac", "BWA pac 索引", "file"),
    ("hg38/reference/Homo_sapiens_assembly38.fasta.64.sa", "BWA sa 索引", "file"),
    ("hg38/reference/Homo_sapiens_assembly38.2bit", "GRCh38 2bit", "file"),
    ("hg38/hla_reference", "HLA 参考库", "directory"),
    ("hg38/blood_tumor/humandb", "血液肿瘤 ANNOVAR humandb", "directory"),
    ("hg38/blood_tumor/humandb/hg38_refGeneWithVer.txt", "hg38 RefGeneWithVer 注释", "file"),
    ("hg38/blood_tumor/bed/genome_windows.bed", "全基因组窗口 BED", "file"),
    ("hg38/blood_tumor/bed/exons_hg38.bed", "GRCh38 外显子 BED", "file"),
    ("hg38/blood_tumor/bed/kszy_84panel.hg38.gene.list", "84 Panel 基因列表", "file"),
    ("hg38/blood_tumor/bed/kszy_BloodTumor_DNA_panel.624.gene.list", "624 Panel 基因列表", "file"),
    ("hg38/blood_tumor/database/ref_annot.gtf", "血液肿瘤参考注释 GTF", "file"),
    ("hg38/blood_tumor/resource/druggable.hg38.csv", "GRCh38 可用药区域", "file"),
    ("hg38/blood_tumor/resource/genomic.gene.gff", "基因 GFF", "file"),
    ("hg38/blood_tumor/resource/sorted.gene.tx.blood.txt", "血液肿瘤转录本映射", "file"),
    ("hg38/blood_tumor/resource/624panel_anno_filter.xls", "624 Panel 注释过滤库", "file"),
    ("hg38/blood_tumor/resource/84panel_filter.xls", "84 Panel 注释过滤库", "file"),
    ("hg38/blood_tumor/resource/cnv_tumor_gene.2024-2.xlsx", "CNV 肿瘤基因库", "file"),
    ("hg38/blood_tumor/resource/dosage_sensitivity_gene.xlsx", "剂量敏感基因库", "file"),
    ("hg38/blood_tumor/resource/GRch38.repeats.coord_noseq.bed", "GRCh38 重复区域", "file"),
    ("hg38/blood_tumor/resource/genomicSuperDups.bed", "Segmental duplication 区域", "file"),
    ("hg38/blood_tumor/resource/panel_CNV_PreClass.xls", "Panel CNV 预分类库", "file"),
    ("hg38/blood_tumor/resource/pre_class.xlsx", "变异预分类库", "file"),
    ("hg38/blood_tumor/resource/special_region.xls", "特殊区域规则库", "file"),
    ("hg38/blood_tumor/resource/20231220/rs.uniq-20231218.in", "化疗位点规则库", "file"),
    ("hg38/blood_tumor/resource/20231220/chemo_efficacy_toxicity_database.sorted.txt", "化疗疗效与毒性数据库", "file"),
    ("hg38/blood_tumor/resource/local_freq_blood/local_freq_blood.zip", "血液肿瘤本地频率库", "file"),
    ("hg38/blood_tumor/resource/local_freq_blood/dna_fusion/84fusion.mutation_frequency.txt", "84 Panel 融合频率库", "file"),
    ("hg38/blood_tumor/resource/local_freq_blood/dna_fusion/624fusion.mutation_frequency.txt", "624 Panel 融合频率库", "file"),
    ("hg38/blood_tumor/cnvkit/84panel", "84 Panel CNVKit 基线", "directory"),
    ("hg38/blood_tumor/cnvkit/396", "396 Panel CNVKit 基线", "directory"),
    ("hg38/blood_tumor/cnvkit_new0919/624panel", "624 Panel CNVKit 基线", "directory"),
    ("hg38/blood_tumor/cnvkit_new0919/624panel_with_100kb_backbone", "624 Panel 100kb backbone 基线", "directory"),
    ("hg38/blood_tumor/cnvdb/84panel/baseline_zm_kz", "84 Panel CNV 数据库", "directory"),
    ("hg38/blood_tumor/cnvdb/624panel_backbone/v2/baseline_624panel_zm_kz", "624 Panel CNV 数据库", "directory"),
    ("hg38/blood_tumor/cnvdb/624panel_backbone/v2/baseline_backbone_zm_kz", "624 backbone CNV 数据库", "directory"),
    ("hg38/annotation/ncbiRefSeqCurated.txt.gz", "RefSeq CNV 注释", "file"),
    ("hg38/annotation/ncbiRefSeqCurated.txt.gz.tbi", "RefSeq CNV 注释索引", "file"),
    ("hg38/annotation/DGV_20200225.txt.gz", "DGV CNV 库", "file"),
    ("hg38/annotation/DGV_20200225.txt.gz.tbi", "DGV CNV 索引", "file"),
    ("hg38/annotation/decipher_population_cnv_grch38.txt.gz", "DECIPHER CNV 库", "file"),
    ("hg38/annotation/decipher_population_cnv_grch38.txt.gz.tbi", "DECIPHER CNV 索引", "file"),
    ("hg38/annotation/variant_summary.txt.gz", "ClinVar CNV 库", "file"),
    ("hg38/annotation/variant_summary.txt.gz.tbi", "ClinVar CNV 索引", "file"),
    ("hg38/annotation/clingen_cnv.tsv.gz", "ClinGen CNV 库", "file"),
    ("hg38/annotation/clingen_cnv.tsv.gz.tbi", "ClinGen CNV 索引", "file"),
    ("hg38/annotation/cytoBandIdeo.txt.gz", "GRCh38 cytoband", "file"),
    ("hg38/annotation/gene_id.txt", "CNV gene id", "file"),
    ("hg38/annotation/representative_transcript.txt", "代表转录本", "file"),
    ("common_db/local_frequency", "项目本地频率库根目录", "directory"),
    ("hg19/resource/combine.tsv", "结果汇总规则", "file"),
    ("hg19/resource/hotspot_gene-20230227.xls", "热点基因库", "file"),
    ("hg19/resource/tumor-gene-20241016.xlsx", "肿瘤基因库", "file"),
    ("hg19/resource/ensembltogenbank.xls", "Ensembl/GenBank 映射", "file"),
    ("hg19/resource/chemo.rs.uniq.120.in", "实体瘤化疗位点规则", "file"),
    ("hg19/resource/chemo.tab1.example", "化疗表一模板", "file"),
    ("hg19/resource/chemo.tab2.example", "化疗表二模板", "file"),
    ("hg19/resource/chemo_efficacy_toxicity_database.txt", "化疗疗效与毒性数据库", "file"),
    ("hg19/resource/chemo_site.bed", "化疗位点 BED", "file"),
    ("hg19/resource/sorted.gene.tx.txt", "基因转录本映射", "file"),
    ("hg19/resource/msidb/microsatellites.list", "MSI 位点库", "file"),
    ("hg19/resource/msidb/reference.list_baseline", "MSI 基线库", "file"),
    ("hg19/database/genomic.gene.gff", "hg19 基因 GFF", "file"),
    ("hg19/humandb/hg19_refGeneWithVer.txt", "hg19 RefGeneWithVer 注释", "file"),
)


def _blood_tumor_hg38_reference() -> dict[str, Any]:
    return {
        "id": "hg38",
        "name": "hg38 / GRCh38（血液肿瘤正式流程）",
        "ref_version": "hg38",
        "required": [
            {"path": path, "label": label, "kind": kind}
            for path, label, kind in BLOOD_TUMOR_HG38_REQUIREMENTS
        ],
    }


def _merge_reference_requirements(
    reference: dict[str, Any], extra: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(reference)
    by_path = {
        str(item.get("path")): item
        for item in [*reference.get("required", []), *extra.get("required", [])]
        if item.get("path")
    }
    merged["required"] = list(by_path.values())
    return merged


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


def _actor_user(request):
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    return None


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _visible_runs(request):
    queryset = AnalysisRun.objects.select_related(
        "asset",
        "revision",
        "workflow_version",
        "workflow_version__workflow",
    )
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
            return queryset
        return queryset.filter(submitted_by=user)
    return queryset.filter(actor="local-user")


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


def _seconds(value: float) -> float:
    return round(max(0.0, value), 3)


@lru_cache(maxsize=256)
def _parse_miniwdl_timing(
    log_path_value: str,
    modified_ns: int,
    file_size: int,
) -> dict[str, Any]:
    del modified_ns, file_size
    workflow_start: float | None = None
    workflow_end: float | None = None
    last_timestamp: float | None = None
    tasks: dict[str, dict[str, Any]] = {}

    with Path(log_path_value).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp_value = (
                event.get("timestamp") if isinstance(event, dict) else None
            )
            if not isinstance(timestamp_value, (int, float)):
                continue
            timestamp = float(timestamp_value)
            last_timestamp = timestamp
            message = str(event.get("message") or "").lower()
            source = str(event.get("source") or "")

            if message == "workflow start" and workflow_start is None:
                workflow_start = timestamp
            elif message == "done" and ".t:" not in source:
                workflow_end = timestamp

            if ".t:" not in source:
                continue
            call_name = source.rsplit(".t:", 1)[-1]
            task = tasks.get(source)
            if message == "task setup":
                if task is None:
                    tasks[source] = {
                        "id": source,
                        "name": str(
                            event.get("name") or call_name.removeprefix("call-")
                        ),
                        "call": call_name,
                        "started_at": timestamp,
                        "finished_at": None,
                        "status": "running",
                        "cached": False,
                    }
                continue
            if task is None:
                continue
            if message.startswith("done"):
                task["finished_at"] = timestamp
                task["status"] = "succeeded"
                task["cached"] = "cached" in message
            elif message in {"failed", "interrupted"} or message.endswith(" failed"):
                task["finished_at"] = timestamp
                task["status"] = "failed"
            elif message == "docker task exit" and event.get("exit_code") not in (
                None,
                0,
            ):
                task["finished_at"] = timestamp
                task["status"] = "failed"

    if workflow_start is None:
        return {"tasks": []}
    observed_end = workflow_end or last_timestamp or workflow_start
    payload_tasks = []
    for task in sorted(tasks.values(), key=lambda item: item["started_at"]):
        end = task["finished_at"] or observed_end
        payload_tasks.append(
            {
                "id": task["id"],
                "name": task["name"],
                "call": task["call"],
                "status": task["status"],
                "cached": task["cached"],
                "offset_seconds": _seconds(task["started_at"] - workflow_start),
                "duration_seconds": _seconds(end - task["started_at"]),
            }
        )
    return {
        "execution_seconds": _seconds(observed_end - workflow_start),
        "task_seconds": _seconds(
            sum(item["duration_seconds"] for item in payload_tasks)
        ),
        "cached_tasks": sum(1 for item in payload_tasks if item["cached"]),
        "tasks": payload_tasks,
    }


def _run_timing_payload(run: AnalysisRun) -> dict[str, Any]:
    timing: dict[str, Any] = {"tasks": []}
    if run.started_at:
        timing["queue_seconds"] = _seconds(
            (run.started_at - run.created_at).total_seconds()
        )
        end = run.finished_at or run.updated_at
        timing["total_seconds"] = _seconds((end - run.started_at).total_seconds())
    if not run.work_directory:
        return timing
    try:
        run_directory = _accessible_run_path(Path(run.work_directory))
    except (OSError, ValueError):
        return timing
    log_path = run_directory / "miniwdl.log"
    try:
        stat = log_path.stat()
        parsed = _parse_miniwdl_timing(str(log_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return timing
    timing.update(parsed)
    return timing


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
            stat = path.stat()
            size = stat.st_size
            total_size += size
            files.append(
                {
                    "mate": mate,
                    "name": path.name,
                    "relative_path": path.relative_to(root).as_posix(),
                    "size": size,
                    "size_label": _format_size(size),
                    "identity": {
                        "size": size,
                        "mtime_ns": stat.st_mtime_ns,
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                    },
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
                present = (
                    candidate.is_dir() if kind == "directory" else candidate.is_file()
                )
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


def _dataset_manifest(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_id": dataset["id"],
        "files": [
            {
                "mate": item["mate"],
                "relative_path": item["relative_path"],
                "verification": "identity",
                **item["identity"],
            }
            for item in dataset["files"]
        ],
    }


def _catalog_resource_manifest(entry: dict[str, Any]) -> dict[str, Any]:
    root = Path(settings.ANALYSIS_DATABASE_ROOT)
    resources = []
    for item in entry.get("required", []):
        if not isinstance(item, dict):
            continue
        candidates = [
            str(item.get("path") or ""),
            *[
                str(path)
                for path in item.get("alternatives", [])
                if isinstance(path, str)
            ],
        ]
        for relative_path in candidates:
            try:
                path = _safe_path(root, relative_path)
                stat = path.stat()
            except (AnalysisInputError, OSError):
                continue
            kind = str(item.get("kind") or "file")
            resource = {
                "relative_path": relative_path,
                "kind": kind,
                "verification": (
                    "exists"
                    if kind == "directory"
                    else ("sha256" if item.get("sha256") else "identity")
                ),
            }
            if kind != "directory":
                resource.update(
                    {
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                    }
                )
            if item.get("sha256"):
                resource["sha256"] = item["sha256"]
            resources.append(resource)
            break
    return {
        "schema_version": 1,
        "resource_id": entry.get("id"),
        "resources": resources,
    }


def _catalog_entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
    requirements = _requirements(entry)
    return {
        key: value
        for key, value in entry.items()
        if key
        not in {
            "directories",
            "required",
            "bed",
            "gene_list",
            "tert_bed",
            "p1q19_bed",
            "druggable_region",
            "cnvkit_db",
        }
    } | {
        "ready": all(item["present"] for item in requirements),
        "requirements": requirements,
        "missing": [item for item in requirements if not item["present"]],
    }


def _workflow_payload(
    slug: str,
    profile: dict[str, str],
    *,
    revision_version: int | None = None,
    available_references: set[str] | None = None,
) -> dict[str, Any]:
    asset = WDLAsset.objects.filter(slug=slug).first()
    latest_revision = asset.source_revisions.first() if asset else None
    revision = (
        asset.source_revisions.filter(version=revision_version).first()
        if asset and revision_version is not None
        else latest_revision
    )
    diagnostics = revision.analysis.get("diagnostics", []) if revision else []
    errors = [item for item in diagnostics if item.get("severity") == "error"]
    historical = bool(
        revision_version is not None
        and (latest_revision is None or revision_version != latest_revision.version)
    )
    workflow_slug = (
        f"{WDL_ASSET_WORKFLOW_PREFIX}{slug}:{revision_version}" if historical else slug
    )
    if asset is None:
        blockers = ["历史 WDL 资产尚未导入。"]
    elif revision is None:
        blockers = [f"WDL revision v{revision_version} 不存在。"]
    elif errors:
        blockers = [
            f"WDL v{revision.version} 有 {len(errors)} 个静态错误，请先在工作台修复。"
        ]
    else:
        blockers = []
    required_reference = profile.get("required_reference")
    if (
        required_reference
        and available_references is not None
        and required_reference not in available_references
    ):
        blockers.append(f"数据库 catalog 尚未配置 {required_reference} 参考资源。")
    if profile.get("input_adapter") == "pending":
        blockers.append("正式流程的运行输入映射尚未配置。")
    return {
        "slug": workflow_slug,
        "source_slug": slug,
        **profile,
        "source_type": "wdl_asset",
        "requires_reference": True,
        "requires_panel": True,
        "required_reference": profile.get("required_reference"),
        "asset_name": asset.name if asset else "",
        "revision": revision.version if revision else None,
        "digest": revision.digest if revision else "",
        "ready": bool(asset and revision and not blockers),
        "diagnostic_count": len(errors),
        "blockers": blockers,
    }


def _managed_wdl_workflows(
    *,
    requested_slug: str = "",
    requested_revision: int | None = None,
    available_references: set[str] | None = None,
) -> list[dict[str, Any]]:
    results = [
        _workflow_payload(
            slug,
            profile,
            available_references=available_references,
        )
        for slug, profile in WORKFLOW_PROFILES.items()
    ]
    profile = WORKFLOW_PROFILES.get(requested_slug)
    if profile is not None and requested_revision is not None:
        requested = _workflow_payload(
            requested_slug,
            profile,
            revision_version=requested_revision,
            available_references=available_references,
        )
        if all(item["slug"] != requested["slug"] for item in results):
            results.append(requested)
    return results


def _parse_wdl_asset_workflow(value: str) -> tuple[str, int | None]:
    if not value.startswith(WDL_ASSET_WORKFLOW_PREFIX):
        return value, None
    try:
        slug, version_value = value.removeprefix(WDL_ASSET_WORKFLOW_PREFIX).rsplit(
            ":", 1
        )
        version = int(version_value)
        if not slug or version < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise AnalysisInputError(
            "ANALYSIS_WORKFLOW_UNSUPPORTED",
            "历史 WDL 运行标识无效。",
        ) from None
    return slug, version


def _workflow_interface(version: WorkflowVersion) -> list[dict[str, Any]]:
    inputs = version.interface_contract.get("inputs")
    if isinstance(inputs, list) and inputs:
        return inputs
    return [
        {
            **(node.get("port") or {}),
            "name": node.get("id"),
            "label": node.get("label") or node.get("id"),
        }
        for node in version.workflow_graph.get("nodes", [])
        if node.get("type") == "workflow_input"
    ]


def _workflow_graph_summary(version: WorkflowVersion) -> dict[str, Any]:
    nodes = version.workflow_graph.get("nodes")
    edges = version.workflow_graph.get("edges")
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []

    tools = []
    subworkflows = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "tool":
            reference = node.get("tool_ref") or {}
            tools.append(
                {
                    "id": reference.get("id") or node.get("id"),
                    "name": node.get("label") or reference.get("id") or node.get("id"),
                    "version": reference.get("tool_version") or "",
                }
            )
        elif node.get("type") == "subworkflow":
            reference = node.get("subworkflow_ref") or {}
            subworkflows.append(
                {
                    "slug": reference.get("slug") or node.get("id"),
                    "name": node.get("label")
                    or reference.get("slug")
                    or node.get("id"),
                    "version": reference.get("version"),
                }
            )

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "input_count": sum(
            node.get("type") == "workflow_input"
            for node in nodes
            if isinstance(node, dict)
        ),
        "tool_count": len(tools),
        "subworkflow_count": len(subworkflows),
        "output_count": sum(
            node.get("type") == "workflow_output"
            for node in nodes
            if isinstance(node, dict)
        ),
        "tools": tools,
        "subworkflows": subworkflows,
    }


def _published_workflow_payload(
    version: WorkflowVersion,
    references: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    interface = _workflow_interface(version)
    blockers = []
    if not version.compiled_bundle or not version.compiled_digest:
        blockers.append("该版本发布时尚未固化编译产物，请重新发布一个新版本。")
    if any(
        node.get("type") == "subworkflow"
        for node in version.workflow_graph.get("nodes", [])
    ):
        blockers.append("当前运行入口暂不支持包含子流程的已发布版本。")
    unsupported = [
        item
        for item in interface
        if item.get("semantic_type") not in SUPPORTED_PUBLISHED_INPUTS
        or (
            item.get("semantic_type") in {"bio.fastq.gz.r1", "bio.fastq.gz.r2"}
            and item.get("wdl_type") != "File"
        )
        or (
            item.get("semantic_type") == "bio.annotation.database_dir"
            and item.get("wdl_type") != "Directory"
        )
    ]
    if unsupported:
        blockers.append(
            "运行页还不能自动构造输入："
            + "、".join(
                str(item.get("label") or item.get("name")) for item in unsupported
            )
        )
    semantics = {item.get("semantic_type") for item in interface}
    if not {"bio.fastq.gz.r1", "bio.fastq.gz.r2"}.issubset(semantics):
        blockers.append("运行页要求已发布流程声明配对 FASTQ 输入语义。")
    key = f"{PUBLISHED_WORKFLOW_PREFIX}{version.workflow.slug}:{version.version}"
    reference_status = {}
    if "bio.annotation.database_dir" in semantics and references is not None:
        for reference in references:
            reference_id = str(reference.get("id") or "")
            try:
                _validate_annotation_reference(version, reference)
                reference_status[reference_id] = _catalog_entry_payload(
                    _annotation_reference_entry(version, reference)
                )
            except AnalysisInputError as error:
                missing = {
                    "path": f"reference/{reference_id}",
                    "label": str(error),
                    "kind": "directory",
                    "present": False,
                }
                reference_status[reference_id] = {
                    "id": reference_id,
                    "name": reference.get("name", reference_id),
                    "ready": False,
                    "requirements": [missing],
                    "missing": [missing],
                }
    return {
        "slug": key,
        "source_type": "workflow_version",
        "source_slug": version.workflow.slug,
        "name": version.name,
        "workflow_name": version.workflow_graph.get("id", version.workflow.slug),
        "mode": "single",
        "description": version.description,
        "asset_name": "",
        "revision": version.version,
        "digest": version.semantic_digest,
        "ready": not blockers,
        "diagnostic_count": len(blockers),
        "blockers": blockers,
        "requires_reference": bool("bio.annotation.database_dir" in semantics),
        "requires_panel": False,
        "reference_status": reference_status,
        "graph_summary": _workflow_graph_summary(version),
    }


def _published_workflows(
    references: list[dict[str, Any]] | None = None,
    *,
    requested_slug: str = "",
    requested_revision: int | None = None,
) -> list[dict[str, Any]]:
    latest_ids = []
    for document in WorkflowDocument.objects.filter(
        kind=WorkflowDocument.Kind.WORKFLOW
    ).prefetch_related("versions"):
        version = document.versions.order_by("-version").first()
        if version is not None:
            latest_ids.append(version.pk)
    if requested_slug and requested_revision is not None:
        requested_id = (
            WorkflowVersion.objects.filter(
                workflow__slug=requested_slug,
                workflow__kind=WorkflowDocument.Kind.WORKFLOW,
                version=requested_revision,
            )
            .values_list("pk", flat=True)
            .first()
        )
        if requested_id is not None and requested_id not in latest_ids:
            latest_ids.append(requested_id)
    versions = WorkflowVersion.objects.select_related("workflow").filter(
        pk__in=latest_ids
    )
    return [
        _published_workflow_payload(version, references)
        for version in sorted(
            versions,
            key=lambda item: (item.workflow.slug, item.version),
        )
    ]


def _parse_published_workflow(value: str) -> WorkflowVersion:
    try:
        slug, version_value = value.removeprefix(PUBLISHED_WORKFLOW_PREFIX).rsplit(
            ":", 1
        )
        version = int(version_value)
    except (TypeError, ValueError):
        raise AnalysisInputError(
            "ANALYSIS_WORKFLOW_UNSUPPORTED",
            "已发布 Workflow 标识无效。",
        ) from None
    item = (
        WorkflowVersion.objects.select_related("workflow")
        .filter(
            workflow__slug=slug,
            version=version,
            kind=WorkflowDocument.Kind.WORKFLOW,
        )
        .first()
    )
    if item is None:
        raise AnalysisInputError(
            "ANALYSIS_WORKFLOW_MISSING",
            "所选已发布 Workflow 版本不存在。",
        )
    payload = _published_workflow_payload(item)
    if not payload["ready"]:
        raise AnalysisInputError(
            "ANALYSIS_WORKFLOW_UNSUPPORTED",
            payload["blockers"][0],
            details={"blockers": payload["blockers"]},
        )
    return item


def _compile_published_workflow(version: WorkflowVersion) -> tuple[dict[str, Any], str]:
    bundle = version.compiled_bundle
    if (
        not isinstance(bundle, dict)
        or _canonical_digest(bundle) != version.compiled_digest
    ):
        raise AnalysisInputError(
            "ANALYSIS_WORKFLOW_INVALID",
            "已发布 Workflow 的固定编译产物校验失败。",
        )
    files = bundle.get("files")
    if not isinstance(files, dict):
        files = {}
    if "workflow.wdl" not in files:
        raise AnalysisInputError(
            "ANALYSIS_WORKFLOW_INVALID",
            "编译结果缺少 workflow.wdl。",
        )
    return bundle, version.compiled_digest


def _published_inputs(
    version: WorkflowVersion,
    dataset: dict[str, Any],
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    read1, read2 = _validate_dataset(dataset)
    execution_paths = {
        "bio.fastq.gz.r1": _execution_path(
            read1,
            Path(settings.ANALYSIS_RAWDATA_ROOT),
            Path(settings.ANALYSIS_RAWDATA_EXECUTION_ROOT),
        ),
        "bio.fastq.gz.r2": _execution_path(
            read2,
            Path(settings.ANALYSIS_RAWDATA_ROOT),
            Path(settings.ANALYSIS_RAWDATA_EXECUTION_ROOT),
        ),
    }
    semantics = {item.get("semantic_type") for item in _workflow_interface(version)}
    if "bio.annotation.database_dir" in semantics:
        if reference is None:
            raise AnalysisInputError(
                "ANALYSIS_REFERENCE_REQUIRED",
                "该流程包含注释数据库目录，请选择参考版本。",
            )
        directories = reference.get("directories")
        if not isinstance(directories, dict) or not directories.get("humandb"):
            raise AnalysisInputError(
                "ANALYSIS_DATABASE_CATALOG_INVALID",
                "参考版本缺少 ANNOVAR humandb 目录映射。",
            )
        database_path = _resource(
            Path(settings.ANALYSIS_DATABASE_ROOT),
            str(directories["humandb"]),
            directory=True,
            execution_root=Path(settings.ANALYSIS_DATABASE_EXECUTION_ROOT),
        )
        execution_paths["bio.annotation.database_dir"] = database_path
    workflow_name = str(version.workflow_graph.get("id") or version.workflow.slug)
    return {
        f"{workflow_name}.{item['name']}": execution_paths[item["semantic_type"]]
        for item in _workflow_interface(version)
    }


def _validate_annotation_reference(
    version: WorkflowVersion,
    reference: dict[str, Any],
) -> None:
    annotation_tool_ids = {
        str(spec.get("id"))
        for spec in version.tool_specs
        if spec.get("task_kind") == "annotation"
    }
    selected_build = str(reference.get("ref_version") or reference.get("id") or "")
    for node in version.workflow_graph.get("nodes", []):
        tool_ref = node.get("tool_ref") or {}
        if tool_ref.get("id") not in annotation_tool_ids:
            continue
        configured_build = str(
            (node.get("parameter_values") or {}).get("ref_version") or ""
        )
        if configured_build != selected_build:
            raise AnalysisInputError(
                "ANALYSIS_REFERENCE_MISMATCH",
                f"注释节点 {node.get('id')} 固定为 {configured_build or '未配置'}，"
                f"与所选参考版本 {selected_build} 不一致。",
            )


def _annotation_reference_entry(
    version: WorkflowVersion,
    reference: dict[str, Any],
) -> dict[str, Any]:
    annotation_specs = {
        str(spec.get("id")): spec
        for spec in version.tool_specs
        if spec.get("task_kind") == "annotation"
    }
    selected_items: set[str] = set()
    for node in version.workflow_graph.get("nodes", []):
        spec = annotation_specs.get(str((node.get("tool_ref") or {}).get("id")))
        if spec is None:
            continue
        configured = (node.get("parameter_values") or {}).get("annotation_items")
        if not isinstance(configured, list):
            selector = next(
                (
                    item
                    for item in spec.get("inputs", [])
                    if item.get("name") == "annotation_items"
                ),
                {},
            )
            configured = selector.get("default", ANNOVAR_CANONICAL_IDS)
        selected_items.update(str(item) for item in configured)

    build = str(reference.get("ref_version") or reference.get("id") or "")
    required = reference.get("required", [])
    scoped_required = [
        item
        for item in required
        if isinstance(item, dict)
        and item.get("kind") == "directory"
        and str(item.get("path") or "").rstrip("/").endswith("/humandb")
    ]
    for item_id in sorted(selected_items):
        patterns = ANNOVAR_RESOURCE_PATTERNS.get(item_id, {}).get(build, [])
        match = next(
            (
                item
                for item in required
                if isinstance(item, dict)
                and any(
                    pattern
                    in " ".join(
                        [
                            str(item.get("path") or ""),
                            *[
                                str(path)
                                for path in item.get("alternatives", [])
                                if isinstance(path, str)
                            ],
                        ]
                    )
                    for pattern in patterns
                )
            ),
            None,
        )
        if match is None:
            scoped_required.append(
                {
                    "path": f"__catalog_missing__/{build}/{item_id}",
                    "kind": "file",
                    "label": f"{item_id}（catalog 未声明）",
                }
            )
        elif match not in scoped_required:
            scoped_required.append(match)
    return {**reference, "required": scoped_required}


def _find_by_id(
    entries: list[dict[str, Any]], entry_id: str, label: str
) -> dict[str, Any]:
    entry = next((item for item in entries if item.get("id") == entry_id), None)
    if entry is None:
        raise AnalysisInputError(
            "ANALYSIS_SELECTION_INVALID",
            f"未找到所选{label}：{entry_id}",
        )
    return entry


def _resource(
    root: Path,
    relative_path: str,
    *,
    directory: bool = False,
    execution_root: Path | None = None,
) -> str:
    path = _safe_path(root, relative_path)
    present = path.is_dir() if directory else path.is_file()
    if not present:
        raise AnalysisInputError(
            "ANALYSIS_DATABASE_INCOMPLETE",
            f"数据库资源缺失：{relative_path}",
            details={"missing": [{"path": relative_path}]},
        )
    if execution_root is None:
        return str(path)
    return str(_safe_path(execution_root, relative_path))


def _execution_path(path: Path, source_root: Path, execution_root: Path) -> str:
    try:
        relative_path = path.resolve().relative_to(source_root.resolve()).as_posix()
    except ValueError as error:
        raise AnalysisInputError(
            "ANALYSIS_PATH_INVALID",
            f"资源路径越过受管目录：{path}",
        ) from error
    return str(_safe_path(execution_root, relative_path))


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
    rawdata_root = Path(settings.ANALYSIS_RAWDATA_ROOT)
    rawdata_execution_root = Path(settings.ANALYSIS_RAWDATA_EXECUTION_ROOT)
    database_root = Path(settings.ANALYSIS_DATABASE_ROOT)
    database_execution_root = Path(settings.ANALYSIS_DATABASE_EXECUTION_ROOT)
    directories = reference.get("directories")
    if not isinstance(directories, dict):
        raise AnalysisInputError(
            "ANALYSIS_DATABASE_CATALOG_INVALID",
            "参考版本缺少 directories 映射。",
        )
    resolved_directories = {
        name: _resource(
            database_root,
            str(path),
            directory=True,
            execution_root=database_execution_root,
        )
        for name, path in directories.items()
    }
    ref_version = str(reference["ref_version"])
    reference_directory = str(directories["reference"]).rstrip("/")
    reference_fasta = _resource(
        database_root,
        f"{reference_directory}/{ref_version}.simp.fa",
        execution_root=database_execution_root,
    )
    reference_fai = _resource(
        database_root,
        f"{reference_directory}/{ref_version}.simp.fa.fai",
        execution_root=database_execution_root,
    )
    resolved_panel = {
        name: _resource(
            database_root,
            str(panel.get(name) or ""),
            directory=name == "cnvkit_db",
            execution_root=database_execution_root,
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
        f"{workflow_name}.ref_version": ref_version,
        f"{workflow_name}.fastq1": _execution_path(
            raw1, rawdata_root, rawdata_execution_root
        ),
        f"{workflow_name}.fastq2": _execution_path(
            raw2, rawdata_root, rawdata_execution_root
        ),
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
                "Collect.fasta": reference_fasta,
                "Collect.fasta_fai": reference_fai,
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
                f"{workflow_name}.fastq3": _execution_path(
                    control_paths[0], rawdata_root, rawdata_execution_root
                ),
                f"{workflow_name}.fastq4": _execution_path(
                    control_paths[1], rawdata_root, rawdata_execution_root
                ),
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


def _accessible_run_path(path: Path) -> Path:
    execution_root = Path(settings.ANALYSIS_RUN_EXECUTION_ROOT).resolve()
    local_root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(execution_root)
    except ValueError:
        # DIND-era runs already contain the container-local root. Preserve access
        # after switching to the host/NAS execution root.
        relative = resolved.relative_to(local_root)
    mapped = (local_root / relative).resolve()
    mapped.relative_to(local_root)
    return mapped


def _output_payload(run: AnalysisRun) -> list[dict[str, Any]]:
    if not run.outputs or not run.work_directory:
        return []
    try:
        root = _accessible_run_path(Path(run.work_directory))
    except (OSError, ValueError):
        return []
    outputs = run.outputs.get("outputs", run.outputs)
    payload = []
    for key, value in _flatten_outputs(outputs):
        if isinstance(value, str):
            path = Path(value)
            try:
                resolved = _accessible_run_path(path)
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


def analysis_run_payload(
    run: AnalysisRun, *, include_events: bool = False
) -> dict[str, Any]:
    if run.workflow_version_id:
        workflow_payload = {
            "slug": run.workflow_version.workflow.slug,
            "name": run.workflow_version.name,
            "workflow_name": run.workflow_name,
            "revision": run.workflow_version.version,
            "digest": run.source_digest,
            "source_type": "workflow_version",
            "graph_summary": _workflow_graph_summary(run.workflow_version),
        }
    else:
        workflow_payload = {
            "slug": run.asset.slug,
            "name": run.asset.name,
            "workflow_name": run.workflow_name,
            "revision": run.revision.version,
            "digest": run.revision.digest,
            "source_type": "wdl_asset",
        }
    payload = {
        "id": str(run.id),
        "workflow": workflow_payload,
        "sample_id": run.sample_id,
        "sample_name": run.sample_name,
        "actor": run.actor,
        "status": run.status,
        "progress": run.progress,
        "current_step": run.current_step,
        "request": run.request_payload,
        "error": run.error,
        "outputs": _output_payload(run),
        "timing": _run_timing_payload(run),
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
    try:
        catalog = load_database_catalog()
        reference_entries = list(catalog["references"])
        declared_reference_ids = {str(item.get("id")) for item in reference_entries}
        blood_reference = _blood_tumor_hg38_reference()
        hg38_index = next(
            (index for index, item in enumerate(reference_entries) if item.get("id") == "hg38"),
            None,
        )
        if hg38_index is None:
            reference_entries.append(blood_reference)
        else:
            reference_entries[hg38_index] = _merge_reference_requirements(
                reference_entries[hg38_index], blood_reference
            )
        references = [_catalog_entry_payload(item) for item in reference_entries]
        panels = [_catalog_entry_payload(item) for item in catalog["panels"]]
        catalog_error = None
    except AnalysisInputError as error:
        references = []
        reference_entries = []
        declared_reference_ids = set()
        panels = []
        catalog_error = {
            "code": error.code,
            "message": str(error),
            "details": error.details,
        }
        catalog = {"schema_version": 1}
    requested_slug = str(request.query_params.get("workflow") or "")
    try:
        requested_revision = int(request.query_params.get("revision"))
    except (TypeError, ValueError):
        requested_revision = None
    if requested_revision is not None and requested_revision < 1:
        requested_revision = None
    workflows = _managed_wdl_workflows(
        requested_slug=requested_slug,
        requested_revision=requested_revision,
        available_references=declared_reference_ids,
    ) + _published_workflows(
        reference_entries,
        requested_slug=requested_slug,
        requested_revision=requested_revision,
    )
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
        runs = _visible_runs(request)[:50]
        return Response({"results": [analysis_run_payload(run) for run in runs]})

    try:
        workflow_slug = str(request.data.get("workflow") or "")
        datasets = discover_fastq_datasets()
        dataset = _find_by_id(
            datasets,
            str(request.data.get("dataset") or ""),
            "原始数据",
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
        if workflow_slug.startswith(PUBLISHED_WORKFLOW_PREFIX):
            workflow_version = _parse_published_workflow(workflow_slug)
            source_bundle, source_digest = _compile_published_workflow(workflow_version)
            interface_semantics = {
                item.get("semantic_type")
                for item in _workflow_interface(workflow_version)
            }
            reference = None
            if "bio.annotation.database_dir" in interface_semantics:
                catalog = load_database_catalog()
                reference = _find_by_id(
                    catalog["references"],
                    str(request.data.get("reference") or ""),
                    "参考版本",
                )
                annotation_reference = _annotation_reference_entry(
                    workflow_version, reference
                )
                requirements = _requirements(annotation_reference)
                if any(not item["present"] for item in requirements):
                    raise AnalysisInputError(
                        "ANALYSIS_DATABASE_INCOMPLETE",
                        "所选参考版本的数据库尚未就绪。",
                        details={
                            "missing": [
                                item for item in requirements if not item["present"]
                            ]
                        },
                    )
                _validate_annotation_reference(workflow_version, reference)
            input_values = _published_inputs(workflow_version, dataset, reference)
            input_manifest = _dataset_manifest(dataset)
            database_manifest = (
                _catalog_resource_manifest(annotation_reference) if reference else None
            )
            run = AnalysisRun.objects.create(
                workflow_version=workflow_version,
                workflow_name=str(
                    workflow_version.workflow_graph.get("id")
                    or workflow_version.workflow.slug
                ),
                sample_id=sample_id,
                sample_name=sample_name,
                actor=_actor(request),
                submitted_by=_actor_user(request),
                source_bundle=source_bundle,
                source_digest=source_digest,
                request_payload={
                    "workflow": workflow_slug,
                    "workflow_semantic_digest": workflow_version.semantic_digest,
                    "compiled_source_digest": source_digest,
                    "input_digest": _canonical_digest(input_manifest),
                    "input_resource_manifest": input_manifest,
                    "database_digest": (
                        _canonical_digest(database_manifest)
                        if database_manifest
                        else None
                    ),
                    "database_resource_manifest": database_manifest,
                    "dataset": dataset["id"],
                    "dataset_name": dataset["name"],
                    "control_dataset_name": None,
                    "reference": reference["id"] if reference else None,
                    "reference_name": (
                        reference.get("name", reference["id"]) if reference else None
                    ),
                    "panel_name": None,
                    "sample_type": str(request.data.get("sample_type") or ""),
                    "sample_gender": str(request.data.get("sample_gender") or ""),
                },
                input_values=input_values,
            )
            AnalysisRunEvent.objects.create(run=run, message="运行已进入队列。")
            return Response(
                analysis_run_payload(run, include_events=True),
                status=status.HTTP_201_CREATED,
            )

        asset_slug, requested_asset_revision = _parse_wdl_asset_workflow(workflow_slug)
        profile = WORKFLOW_PROFILES.get(asset_slug)
        if profile is None:
            raise AnalysisInputError(
                "ANALYSIS_WORKFLOW_UNSUPPORTED",
                "当前运行页只允许已配置的受管 WDL。",
            )
        asset = WDLAsset.objects.filter(slug=asset_slug).first()
        revision = (
            asset.source_revisions.filter(version=requested_asset_revision).first()
            if asset and requested_asset_revision is not None
            else asset.source_revisions.first()
            if asset
            else None
        )
        if asset is None:
            raise AnalysisInputError(
                "ANALYSIS_WORKFLOW_MISSING",
                "所选历史 WDL 尚未导入。",
            )
        if revision is None:
            raise AnalysisInputError(
                "ANALYSIS_WORKFLOW_MISSING",
                f"所选 WDL revision v{requested_asset_revision} 不存在。",
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
        return _error(
            error.code, str(error), status.HTTP_400_BAD_REQUEST, details=error.details
        )

    input_manifest = {
        "primary": _dataset_manifest(dataset),
        "control": _dataset_manifest(control) if control else None,
    }
    reference_manifest = _catalog_resource_manifest(reference)
    panel_manifest = _catalog_resource_manifest(panel)
    run = AnalysisRun.objects.create(
        asset=asset,
        revision=revision,
        workflow_name=profile["workflow_name"],
        sample_id=sample_id,
        sample_name=sample_name,
        actor=_actor(request),
        submitted_by=_actor_user(request),
        request_payload={
            "workflow": workflow_slug,
            "wdl_revision": revision.version,
            "wdl_revision_digest": revision.digest,
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
            "database_catalog_digest": _canonical_digest(catalog),
            "reference_digest": _canonical_digest(reference_manifest),
            "panel_digest": _canonical_digest(panel_manifest),
            "input_digest": _canonical_digest(input_manifest),
            "input_resource_manifest": input_manifest,
            "reference_resource_manifest": reference_manifest,
            "panel_resource_manifest": panel_manifest,
        },
        input_values=input_values,
    )
    AnalysisRunEvent.objects.create(run=run, message="运行已进入队列。")
    return Response(
        analysis_run_payload(run, include_events=True), status=status.HTTP_201_CREATED
    )


@api_view(["GET"])
def analysis_run_detail(request, run_id):
    run = get_object_or_404(
        _visible_runs(request).prefetch_related("events"),
        pk=run_id,
    )
    return Response(analysis_run_payload(run, include_events=True))


@api_view(["GET"])
def analysis_run_output(request, run_id):
    run = get_object_or_404(_visible_runs(request), pk=run_id)
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
        root = _accessible_run_path(Path(run.work_directory))
        path = _accessible_run_path(Path(values[key]))
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

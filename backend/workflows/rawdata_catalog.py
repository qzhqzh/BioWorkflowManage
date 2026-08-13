from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.response import Response


FASTQ_PATTERN = re.compile(
    r"^(?P<prefix>.+?)(?P<marker>[_\.-]R)(?P<mate>[12])(?P<suffix>(?:[_\.-].*)?)\.(?P<extension>fastq|fq)\.gz$",
    re.IGNORECASE,
)
FASTQ_SUFFIX_PATTERN = re.compile(r"\.(?:fastq|fq)\.gz$", re.IGNORECASE)
MAX_SCANNED_FILES = 2000
MAX_SCANNED_ENTRIES = 10000
MAX_RELATIVE_PARTS = 4
SCAN_DEADLINE_SECONDS = 3.0
CATALOG_CACHE_TTL_SECONDS = 5.0
CATALOG_FORCE_REFRESH_INTERVAL_SECONDS = 2.0

_catalog_cache_lock = threading.Lock()
_catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}


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


def _modified_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _file_payload(path: Path, root: Path, mate: int | None = None) -> dict[str, Any]:
    stat = path.stat()
    size = stat.st_size
    payload: dict[str, Any] = {
        "name": path.name,
        "relative_path": path.relative_to(root).as_posix(),
        "size": size,
        "size_label": _format_size(size),
        "modified_at": _modified_at(stat.st_mtime),
        "identity": {
            "size": size,
            "mtime_ns": stat.st_mtime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
        },
    }
    if mate is not None:
        payload["mate"] = mate
    return payload


def _issue(code: str, message: str, *, path: str = "") -> dict[str, str]:
    payload = {"code": code, "message": message}
    if path:
        payload["path"] = path
    return payload


def _empty_catalog(root_status: str, issue: dict[str, str]) -> dict[str, Any]:
    return {
        "root_directory": "workspace/rawdata",
        "root_status": root_status,
        "scanned_at": datetime.now(tz=UTC).isoformat(),
        "scan_limited": False,
        "scan_limit": MAX_SCANNED_FILES,
        "scan_entry_limit": MAX_SCANNED_ENTRIES,
        "scanned_entry_count": 0,
        "summary": {
            "file_count": 0,
            "dataset_count": 0,
            "ready_dataset_count": 0,
            "issue_dataset_count": 0,
            "unrecognized_fastq_count": 0,
            "total_size": 0,
            "total_size_label": "0 B",
        },
        "directories": [],
        "datasets": [],
        "unrecognized_files": [],
        "issues": [issue],
    }


def scan_rawdata_catalog(
    root_value: str | Path,
    *,
    max_files: int = MAX_SCANNED_FILES,
    max_entries: int = MAX_SCANNED_ENTRIES,
    deadline_seconds: float = SCAN_DEADLINE_SECONDS,
) -> dict[str, Any]:
    root = Path(root_value)
    if not root.exists():
        return _empty_catalog(
            "missing",
            _issue("RAWDATA_ROOT_MISSING", "原始数据目录尚未挂载或不存在。"),
        )
    if not root.is_dir():
        return _empty_catalog(
            "unreadable",
            _issue("RAWDATA_ROOT_INVALID", "原始数据路径不是目录。"),
        )

    pairs: dict[str, dict[int, list[dict[str, Any]]]] = {}
    pair_stems: dict[str, str] = {}
    unrecognized_files: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    scanned_files = 0
    scanned_entries = 0
    total_fastq_size = 0
    scan_limited = False

    def on_walk_error(error: OSError) -> None:
        relative = ""
        filename = getattr(error, "filename", "")
        if filename:
            try:
                relative = Path(filename).relative_to(root).as_posix()
            except ValueError:
                relative = ""
        issues.append(
            _issue("RAWDATA_DIRECTORY_UNREADABLE", "部分目录无法读取。", path=relative)
        )

    started_at = time.monotonic()
    try:
        pending_directories = [root]
        while pending_directories and not scan_limited:
            current = pending_directories.pop()
            try:
                relative_directory = current.relative_to(root)
                entries = os.scandir(current)
            except (OSError, ValueError) as error:
                if isinstance(error, OSError):
                    on_walk_error(error)
                continue
            with entries:
                for entry in entries:
                    if (
                        scanned_entries >= max_entries
                        or time.monotonic() - started_at >= deadline_seconds
                    ):
                        scan_limited = True
                        break
                    scanned_entries += 1
                    path = Path(entry.path)
                    relative_path = path.relative_to(root).as_posix()
                    try:
                        if entry.is_symlink():
                            if FASTQ_SUFFIX_PATTERN.search(entry.name):
                                issues.append(
                                    _issue(
                                        "RAWDATA_SYMLINK_IGNORED",
                                        "符号链接 FASTQ 未纳入运行数据。",
                                        path=relative_path,
                                    )
                                )
                            else:
                                issues.append(
                                    _issue(
                                        "RAWDATA_SYMLINK_IGNORED",
                                        "符号链接目录未扫描。",
                                        path=relative_path,
                                    )
                                )
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if len(relative_directory.parts) < MAX_RELATIVE_PARTS - 1:
                                pending_directories.append(path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        issues.append(
                            _issue(
                                "RAWDATA_FILE_UNREADABLE",
                                "目录项无法读取。",
                                path=relative_path,
                            )
                        )
                        continue
                    if scanned_files >= max_files:
                        scan_limited = True
                        break
                    scanned_files += 1
                    if not FASTQ_SUFFIX_PATTERN.search(entry.name):
                        continue
                    try:
                        payload = _file_payload(path, root)
                    except OSError:
                        issues.append(
                            _issue(
                                "RAWDATA_FILE_UNREADABLE",
                                "FASTQ 文件无法读取。",
                                path=relative_path,
                            )
                        )
                        continue
                    total_fastq_size += payload["size"]
                    match = FASTQ_PATTERN.match(entry.name)
                    if not match:
                        unrecognized_files.append(payload)
                        continue
                    mate = int(match.group("mate"))
                    payload["mate"] = mate
                    pair_name = (
                        f"{match.group('prefix')}{match.group('marker')}{{R}}"
                        f"{match.group('suffix')}.{match.group('extension').lower()}.gz"
                    )
                    key = str(relative_directory / pair_name)
                    pairs.setdefault(key, {}).setdefault(mate, []).append(payload)
                    pair_stems[key] = match.group("prefix")
    except OSError:
        return _empty_catalog(
            "unreadable",
            _issue("RAWDATA_ROOT_UNREADABLE", "原始数据目录无法读取。"),
        )

    unrecognized_files.sort(key=lambda item: item["relative_path"])
    datasets: list[dict[str, Any]] = []
    directory_map: dict[str, dict[str, Any]] = {}
    for key, mates in sorted(pairs.items()):
        files = sorted(
            (item for mate in (1, 2) for item in mates.get(mate, [])),
            key=lambda item: item["relative_path"],
        )
        dataset_issues: list[dict[str, str]] = []
        missing_mates = [mate for mate in (1, 2) if not mates.get(mate)]
        if scan_limited:
            dataset_issues.append(
                _issue(
                    "RAWDATA_SCAN_INCOMPLETE",
                    "扫描未完成，暂时无法确认配对与重复文件。",
                )
            )
        else:
            for mate in missing_mates:
                dataset_issues.append(
                    _issue("RAWDATA_MATE_MISSING", f"缺少 R{mate} 配对文件。")
                )
            for mate in (1, 2):
                if len(mates.get(mate, [])) > 1:
                    dataset_issues.append(
                        _issue(
                            "RAWDATA_MATE_DUPLICATED",
                            f"检测到多个 R{mate} 文件。",
                        )
                    )
        for item in files:
            if item["size"] == 0:
                dataset_issues.append(
                    _issue(
                        "RAWDATA_FILE_EMPTY",
                        f"{item['name']} 是空文件。",
                        path=item["relative_path"],
                    )
                )
        status = (
            "scan_incomplete"
            if scan_limited
            else "ready"
            if not dataset_issues
            else "issue"
        )
        total_size = sum(item["size"] for item in files)
        directory = Path(key).parent.as_posix()
        if directory == ".":
            directory = "根目录"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        dataset = {
            "id": digest,
            "name": _sample_code(pair_stems[key]),
            "pair_key": key,
            "directory": directory,
            "status": status,
            "missing_mates": [] if scan_limited else missing_mates,
            "issues": dataset_issues,
            "files": files,
            "total_size": total_size,
            "total_size_label": _format_size(total_size),
        }
        datasets.append(dataset)
        directory_entry = directory_map.setdefault(
            directory,
            {
                "path": directory,
                "dataset_count": 0,
                "ready_count": 0,
                "issue_count": 0,
                "unrecognized_count": 0,
                "total_size": 0,
            },
        )
        directory_entry["dataset_count"] += 1
        if status == "ready":
            directory_entry["ready_count"] += 1
        else:
            directory_entry["issue_count"] += 1
        directory_entry["total_size"] += total_size

    for item in unrecognized_files:
        directory = Path(item["relative_path"]).parent.as_posix()
        if directory == ".":
            directory = "根目录"
        directory_entry = directory_map.setdefault(
            directory,
            {
                "path": directory,
                "dataset_count": 0,
                "ready_count": 0,
                "issue_count": 0,
                "unrecognized_count": 0,
                "total_size": 0,
            },
        )
        directory_entry["unrecognized_count"] += 1
        directory_entry["total_size"] += item["size"]

    if scan_limited:
        issues.append(
            _issue(
                "RAWDATA_SCAN_LIMIT_REACHED",
                "本次扫描达到安全预算，结果可能不完整；请缩小目录范围后重试。",
            )
        )
    directories = sorted(directory_map.values(), key=lambda item: item["path"])
    for item in directories:
        item["total_size_label"] = _format_size(item["total_size"])
    ready_count = sum(item["status"] == "ready" for item in datasets)
    return {
        "root_directory": "workspace/rawdata",
        "root_status": "ready",
        "scanned_at": datetime.now(tz=UTC).isoformat(),
        "scan_limited": scan_limited,
        "scan_limit": max_files,
        "scan_entry_limit": max_entries,
        "scanned_entry_count": scanned_entries,
        "summary": {
            "file_count": sum(len(item["files"]) for item in datasets)
            + len(unrecognized_files),
            "dataset_count": len(datasets),
            "ready_dataset_count": ready_count,
            "issue_dataset_count": len(datasets) - ready_count,
            "unrecognized_fastq_count": len(unrecognized_files),
            "total_size": total_fastq_size,
            "total_size_label": _format_size(total_fastq_size),
        },
        "directories": directories,
        "datasets": datasets,
        "unrecognized_files": unrecognized_files,
        "issues": issues,
    }


def runnable_fastq_datasets(root_value: str | Path) -> list[dict[str, Any]]:
    return [
        item
        for item in cached_rawdata_catalog(root_value)["datasets"]
        if item["status"] == "ready"
    ]


def public_rawdata_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    payload = dict(catalog)
    payload["datasets"] = [
        {
            **dataset,
            "files": [
                {key: value for key, value in item.items() if key != "identity"}
                for item in dataset["files"]
            ],
        }
        for dataset in catalog["datasets"]
    ]
    payload["unrecognized_files"] = [
        {key: value for key, value in item.items() if key != "identity"}
        for item in catalog["unrecognized_files"]
    ]
    return payload


def cached_rawdata_catalog(
    root_value: str | Path,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    cache_key = str(Path(root_value).resolve(strict=False))
    now = time.monotonic()
    with _catalog_cache_lock:
        cached = _catalog_cache.get(cache_key)
        if cached:
            cached_at, payload = cached
            minimum_age = (
                CATALOG_FORCE_REFRESH_INTERVAL_SECONDS
                if force_refresh
                else CATALOG_CACHE_TTL_SECONDS
            )
            if now - cached_at < minimum_age:
                return payload
        payload = scan_rawdata_catalog(root_value)
        _catalog_cache[cache_key] = (time.monotonic(), payload)
        return payload


@api_view(["GET"])
def rawdata_catalog(request):
    force_refresh = str(request.query_params.get("refresh") or "").lower() in {
        "1",
        "true",
    }
    response = Response(
        public_rawdata_catalog(
            cached_rawdata_catalog(
                settings.ANALYSIS_RAWDATA_ROOT,
                force_refresh=force_refresh,
            )
        )
    )
    response["Cache-Control"] = "no-store"
    return response

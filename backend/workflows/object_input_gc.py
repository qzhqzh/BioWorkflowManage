from __future__ import annotations

import os
import re
import stat as stat_module
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import AnalysisRun, InputStagingCoordinator, InputStagingLease
from .object_inputs import (
    ObjectInputError,
    STAGING_DIRECTORY_FLAGS,
    object_manifest_items,
)


ACTIVE_RUN_STATES = {
    AnalysisRun.Status.QUEUED,
    AnalysisRun.Status.PREPARING,
    AnalysisRun.Status.RUNNING,
    AnalysisRun.Status.CANCEL_REQUESTED,
}
CACHE_BUCKET_PATTERN = re.compile(r"^[0-9a-f]{2}$")
CACHE_FILE_PATTERN = re.compile(
    r"^(?P<digest>[0-9a-f]{64})(?P<suffix>(?:\.[a-z0-9]{1,10}){0,3})$"
)


class ObjectInputCacheGCError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class CacheFile:
    relative_path: str
    bucket: str
    name: str
    size: int
    mtime: datetime
    identity: tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class CacheScan:
    root_entries: tuple[str, ...]
    sha256_present: bool
    bucket_names: tuple[str, ...]
    files: tuple[CacheFile, ...]


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int
    free: int


def _gc_error(code: str, message: str, *, path: str | None = None):
    details = {"path": path} if path else None
    return ObjectInputCacheGCError(code, message, details=details)


def _metadata_identity(metadata) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_nlink),
    )


def _open_gc_root() -> tuple[int, int]:
    configured = Path(settings.ANALYSIS_INPUT_STAGING_ROOT)
    descriptor = -1
    try:
        root = configured.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_ROOT_UNAVAILABLE",
            "对象输入暂存根目录不存在或不可访问。",
        ) from error
    if root == Path("/"):
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_PATH_UNSAFE",
            "对象输入缓存 GC 根目录不能是文件系统根目录。",
        )
    try:
        descriptor = os.open(configured, STAGING_DIRECTORY_FLAGS)
        metadata = os.fstat(descriptor)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_ROOT_UNAVAILABLE",
            "对象输入暂存根目录无法安全打开。",
        ) from error
    if not stat_module.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_PATH_UNSAFE",
            "对象输入暂存根路径不是普通目录。",
        )
    return descriptor, int(metadata.st_dev)


def _open_child_directory(
    parent: int,
    name: str,
    *,
    expected_device: int,
    relative_path: str,
) -> int:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat_module.S_ISDIR(before.st_mode) or before.st_dev != expected_device:
            raise OSError("cache directory is unsafe")
        descriptor = os.open(name, STAGING_DIRECTORY_FLAGS, dir_fd=parent)
        after = os.fstat(descriptor)
        if (
            not stat_module.S_ISDIR(after.st_mode)
            or after.st_dev != expected_device
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
        ):
            raise OSError("cache directory changed")
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_PATH_UNSAFE",
            "对象输入缓存目录包含符号链接、跨设备目录或并发变化。",
            path=relative_path,
        ) from error


def _cache_filename_is_valid(bucket: str, name: str) -> bool:
    match = CACHE_FILE_PATTERN.fullmatch(name)
    return bool(
        match
        and match.group("digest").startswith(bucket)
        and len(match.group("suffix")) <= 32
    )


def _disk_usage(root_descriptor: int) -> DiskUsage:
    try:
        usage = os.fstatvfs(root_descriptor)
    except OSError as error:
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_CAPACITY_UNAVAILABLE",
            "无法读取对象输入暂存文件系统容量。",
        ) from error
    block_size = int(usage.f_frsize or usage.f_bsize)
    return DiskUsage(
        total=int(usage.f_blocks) * block_size,
        used=(int(usage.f_blocks) - int(usage.f_bfree)) * block_size,
        free=int(usage.f_bavail) * block_size,
    )


def _scan_cache_files(root_descriptor: int, *, expected_device: int) -> CacheScan:
    try:
        root_entries = tuple(sorted(os.listdir(root_descriptor)))
    except OSError as error:
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_SCAN_FAILED",
            "无法读取对象输入暂存根目录。",
        ) from error
    unknown_root_entries = set(root_entries) - {".leases", "sha256"}
    if unknown_root_entries:
        name = sorted(unknown_root_entries)[0]
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_UNKNOWN_NODE",
            "对象输入暂存根目录包含未知节点，未执行删除。",
            path=name,
        )
    for structural_name in (".leases", "sha256"):
        if structural_name not in root_entries:
            continue
        descriptor = _open_child_directory(
            root_descriptor,
            structural_name,
            expected_device=expected_device,
            relative_path=structural_name,
        )
        os.close(descriptor)
    if "sha256" not in root_entries:
        return CacheScan(root_entries, False, (), ())

    sha256_descriptor = _open_child_directory(
        root_descriptor,
        "sha256",
        expected_device=expected_device,
        relative_path="sha256",
    )
    files: list[CacheFile] = []
    try:
        try:
            bucket_names = tuple(sorted(os.listdir(sha256_descriptor)))
        except OSError as error:
            raise _gc_error(
                "OBJECT_INPUT_CACHE_GC_SCAN_FAILED",
                "无法读取对象输入缓存内容目录。",
                path="sha256",
            ) from error
        for bucket in bucket_names:
            if not CACHE_BUCKET_PATTERN.fullmatch(bucket):
                raise _gc_error(
                    "OBJECT_INPUT_CACHE_GC_UNKNOWN_NODE",
                    "对象输入缓存包含未知摘要目录，未执行删除。",
                    path=f"sha256/{bucket}",
                )
            bucket_descriptor = _open_child_directory(
                sha256_descriptor,
                bucket,
                expected_device=expected_device,
                relative_path=f"sha256/{bucket}",
            )
            try:
                try:
                    names = tuple(sorted(os.listdir(bucket_descriptor)))
                except OSError as error:
                    raise _gc_error(
                        "OBJECT_INPUT_CACHE_GC_SCAN_FAILED",
                        "无法读取对象输入缓存摘要目录。",
                        path=f"sha256/{bucket}",
                    ) from error
                for name in names:
                    relative_path = f"sha256/{bucket}/{name}"
                    if not _cache_filename_is_valid(bucket, name):
                        raise _gc_error(
                            "OBJECT_INPUT_CACHE_GC_UNKNOWN_NODE",
                            "对象输入缓存包含未知内容节点，未执行删除。",
                            path=relative_path,
                        )
                    try:
                        metadata = os.stat(
                            name,
                            dir_fd=bucket_descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        raise _gc_error(
                            "OBJECT_INPUT_CACHE_GC_SCAN_FAILED",
                            "对象输入缓存内容无法安全检查。",
                            path=relative_path,
                        ) from error
                    if (
                        not stat_module.S_ISREG(metadata.st_mode)
                        or metadata.st_dev != expected_device
                        or metadata.st_mode & 0o222
                        or metadata.st_nlink != 1
                    ):
                        raise _gc_error(
                            "OBJECT_INPUT_CACHE_GC_PATH_UNSAFE",
                            "对象输入缓存内容不是只读、单链接的同设备普通文件。",
                            path=relative_path,
                        )
                    files.append(
                        CacheFile(
                            relative_path=relative_path,
                            bucket=bucket,
                            name=name,
                            size=int(metadata.st_size),
                            mtime=datetime.fromtimestamp(
                                metadata.st_mtime,
                                tz=datetime_timezone.utc,
                            ),
                            identity=_metadata_identity(metadata),
                        )
                    )
                    if len(files) > int(
                        settings.ANALYSIS_OBJECT_STAGE_GC_SCAN_MAX_FILES
                    ):
                        raise _gc_error(
                            "OBJECT_INPUT_CACHE_GC_SCAN_LIMIT",
                            "对象输入缓存文件数超过 GC 安全扫描上限。",
                        )
            finally:
                os.close(bucket_descriptor)
    finally:
        os.close(sha256_descriptor)
    return CacheScan(root_entries, True, bucket_names, tuple(files))


def _manifest_paths(payload: Any, *, run_id: Any) -> set[str]:
    manifest = payload.get("input_resource_manifest") if isinstance(payload, dict) else None
    try:
        items = object_manifest_items(manifest)
    except ObjectInputError as error:
        raise ObjectInputCacheGCError(
            "OBJECT_INPUT_CACHE_GC_REFERENCE_INVALID",
            "AnalysisRun 对象输入清单无效，GC 已失败关闭。",
            details={"run_id": str(run_id), "error_code": error.code},
        ) from error
    return {str(item["staging_relative_path"]) for item in items}


def _protected_paths(*, now, cutoff) -> dict[str, set[str]]:
    protected: dict[str, set[str]] = defaultdict(set)
    runs = (
        AnalysisRun.objects.filter(
            Q(status__in=ACTIVE_RUN_STATES) | Q(updated_at__gt=cutoff)
        )
        .values("id", "status", "request_payload")
        .iterator(chunk_size=500)
    )
    for run in runs:
        reason = (
            "active_run"
            if run["status"] in ACTIVE_RUN_STATES
            else "recent_terminal_run"
        )
        for path in _manifest_paths(run["request_payload"], run_id=run["id"]):
            protected[path].add(reason)
    leases = (
        InputStagingLease.objects.filter(expires_at__gt=now)
        .values("run_id", "run__request_payload")
        .iterator(chunk_size=100)
    )
    for lease in leases:
        for path in _manifest_paths(
            lease["run__request_payload"],
            run_id=lease["run_id"],
        ):
            protected[path].add("active_lease")
    return protected


def _protection_reason(reasons: set[str]) -> str | None:
    for reason in ("active_lease", "active_run", "recent_terminal_run"):
        if reason in reasons:
            return reason
    return None


def _validate_scan_snapshot(
    root_descriptor: int,
    scan: CacheScan,
    *,
    expected_device: int,
) -> None:
    try:
        if tuple(sorted(os.listdir(root_descriptor))) != scan.root_entries:
            raise OSError("staging root changed")
    except OSError as error:
        raise _gc_error(
            "OBJECT_INPUT_CACHE_GC_RACE",
            "对象输入缓存根目录在 GC 期间发生变化。",
        ) from error
    if not scan.sha256_present:
        return
    sha256_descriptor = _open_child_directory(
        root_descriptor,
        "sha256",
        expected_device=expected_device,
        relative_path="sha256",
    )
    expected_by_bucket: dict[str, dict[str, CacheFile]] = defaultdict(dict)
    for item in scan.files:
        expected_by_bucket[item.bucket][item.name] = item
    try:
        try:
            current_buckets = tuple(sorted(os.listdir(sha256_descriptor)))
        except OSError as error:
            raise _gc_error(
                "OBJECT_INPUT_CACHE_GC_RACE",
                "对象输入缓存摘要目录在 GC 期间无法复核。",
                path="sha256",
            ) from error
        if current_buckets != scan.bucket_names:
            raise _gc_error(
                "OBJECT_INPUT_CACHE_GC_RACE",
                "对象输入缓存摘要目录在 GC 期间发生变化。",
                path="sha256",
            )
        for bucket in scan.bucket_names:
            bucket_descriptor = _open_child_directory(
                sha256_descriptor,
                bucket,
                expected_device=expected_device,
                relative_path=f"sha256/{bucket}",
            )
            try:
                expected = expected_by_bucket[bucket]
                current_names = tuple(sorted(os.listdir(bucket_descriptor)))
                if current_names != tuple(sorted(expected)):
                    raise _gc_error(
                        "OBJECT_INPUT_CACHE_GC_RACE",
                        "对象输入缓存内容在 GC 期间发生变化。",
                        path=f"sha256/{bucket}",
                    )
                for name, item in expected.items():
                    metadata = os.stat(
                        name,
                        dir_fd=bucket_descriptor,
                        follow_symlinks=False,
                    )
                    if _metadata_identity(metadata) != item.identity:
                        raise _gc_error(
                            "OBJECT_INPUT_CACHE_GC_RACE",
                            "对象输入缓存文件在 GC 期间发生变化。",
                            path=item.relative_path,
                        )
            except ObjectInputCacheGCError:
                raise
            except OSError as error:
                raise _gc_error(
                    "OBJECT_INPUT_CACHE_GC_RACE",
                    "对象输入缓存内容在 GC 期间无法复核。",
                    path=f"sha256/{bucket}",
                ) from error
            finally:
                os.close(bucket_descriptor)
    finally:
        os.close(sha256_descriptor)


def _delete_selected_files(
    root_descriptor: int,
    scan: CacheScan,
    selected: list[CacheFile],
    *,
    expected_device: int,
) -> tuple[int, int]:
    if not scan.sha256_present:
        return 0, 0
    selected_by_bucket: dict[str, list[CacheFile]] = defaultdict(list)
    for item in selected:
        selected_by_bucket[item.bucket].append(item)
    scanned_by_bucket: dict[str, tuple[str, ...]] = defaultdict(tuple)
    grouped_names: dict[str, list[str]] = defaultdict(list)
    for item in scan.files:
        grouped_names[item.bucket].append(item.name)
    for bucket, names in grouped_names.items():
        scanned_by_bucket[bucket] = tuple(sorted(names))

    sha256_descriptor = _open_child_directory(
        root_descriptor,
        "sha256",
        expected_device=expected_device,
        relative_path="sha256",
    )
    released_bytes = 0
    removed_directories = 0
    try:
        for bucket in scan.bucket_names:
            bucket_descriptor = _open_child_directory(
                sha256_descriptor,
                bucket,
                expected_device=expected_device,
                relative_path=f"sha256/{bucket}",
            )
            empty = False
            try:
                if tuple(sorted(os.listdir(bucket_descriptor))) != scanned_by_bucket[bucket]:
                    raise _gc_error(
                        "OBJECT_INPUT_CACHE_GC_RACE",
                        "对象输入缓存内容在删除前发生变化。",
                        path=f"sha256/{bucket}",
                    )
                for item in selected_by_bucket.get(bucket, []):
                    try:
                        metadata = os.stat(
                            item.name,
                            dir_fd=bucket_descriptor,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        raise _gc_error(
                            "OBJECT_INPUT_CACHE_GC_RACE",
                            "对象输入缓存文件在删除前消失或变化。",
                            path=item.relative_path,
                        ) from error
                    if _metadata_identity(metadata) != item.identity:
                        raise _gc_error(
                            "OBJECT_INPUT_CACHE_GC_RACE",
                            "对象输入缓存文件在删除前发生变化。",
                            path=item.relative_path,
                        )
                    try:
                        os.unlink(item.name, dir_fd=bucket_descriptor)
                    except OSError as error:
                        raise _gc_error(
                            "OBJECT_INPUT_CACHE_GC_DELETE_FAILED",
                            "对象输入缓存文件删除失败。",
                            path=item.relative_path,
                        ) from error
                    released_bytes += item.size
                os.fsync(bucket_descriptor)
                empty = not os.listdir(bucket_descriptor)
            finally:
                os.close(bucket_descriptor)
            if empty:
                try:
                    os.rmdir(bucket, dir_fd=sha256_descriptor)
                    os.fsync(sha256_descriptor)
                except OSError as error:
                    raise _gc_error(
                        "OBJECT_INPUT_CACHE_GC_DELETE_FAILED",
                        "对象输入缓存空摘要目录删除失败。",
                        path=f"sha256/{bucket}",
                    ) from error
                removed_directories += 1
    finally:
        os.close(sha256_descriptor)
    return released_bytes, removed_directories


def garbage_collect_object_input_cache(
    *,
    apply: bool = False,
    all_eligible: bool = False,
    limit: int | None = None,
    actor: str = "deployment",
) -> dict[str, Any]:
    maximum = int(settings.ANALYSIS_OBJECT_STAGE_GC_MAX_FILES)
    requested_limit = maximum if limit is None else int(limit)
    if requested_limit < 1:
        raise ObjectInputCacheGCError(
            "OBJECT_INPUT_CACHE_GC_LIMIT_INVALID",
            "对象输入缓存 GC limit 必须大于 0。",
        )
    effective_limit = min(maximum, requested_limit)
    retention_days = int(settings.ANALYSIS_OBJECT_STAGE_RETENTION_DAYS)
    high_percent = int(settings.ANALYSIS_OBJECT_STAGE_GC_HIGH_WATER_PERCENT)
    low_percent = int(settings.ANALYSIS_OBJECT_STAGE_GC_LOW_WATER_PERCENT)
    now = timezone.now()
    cutoff = now - timedelta(days=retention_days)

    root_descriptor, expected_device = _open_gc_root()
    try:
        with transaction.atomic():
            InputStagingCoordinator.objects.select_for_update().get(pk=1)
            before = _disk_usage(root_descriptor)
            scan = _scan_cache_files(
                root_descriptor,
                expected_device=expected_device,
            )
            protected = _protected_paths(now=now, cutoff=cutoff)
            skipped = Counter()
            eligible: list[CacheFile] = []
            for item in scan.files:
                reason = _protection_reason(protected.get(item.relative_path, set()))
                if reason is not None:
                    skipped[reason] += 1
                elif item.mtime > cutoff:
                    skipped["retention_period_active"] += 1
                else:
                    eligible.append(item)
            eligible.sort(key=lambda item: (item.mtime, item.relative_path))

            high_bytes = (before.total * high_percent + 99) // 100
            low_bytes = (before.total * low_percent) // 100
            watermark_triggered = before.used >= high_bytes
            required_release = max(0, before.used - low_bytes)
            desired: list[CacheFile] = []
            if all_eligible:
                desired = eligible
            elif watermark_triggered:
                planned_bytes = 0
                for item in eligible:
                    if planned_bytes >= required_release:
                        skipped["low_water_reached"] += 1
                        continue
                    desired.append(item)
                    planned_bytes += item.size
            else:
                skipped["below_high_water"] += len(eligible)

            selected = desired[:effective_limit]
            if len(desired) > len(selected):
                skipped["limit_reached"] += len(desired) - len(selected)
            selected_bytes = sum(item.size for item in selected)
            released_bytes = 0
            removed_directories = 0
            if apply:
                _validate_scan_snapshot(
                    root_descriptor,
                    scan,
                    expected_device=expected_device,
                )
                released_bytes, removed_directories = _delete_selected_files(
                    root_descriptor,
                    scan,
                    selected,
                    expected_device=expected_device,
                )
            after = _disk_usage(root_descriptor)
    finally:
        os.close(root_descriptor)

    cache_bytes_before = sum(item.size for item in scan.files)
    projected_used_bytes = max(0, before.used - selected_bytes)
    result = {
        "schema_version": 1,
        "mode": "apply" if apply else "dry_run",
        "actor": str(actor or "deployment")[:256],
        "all_eligible": bool(all_eligible),
        "retention_days": retention_days,
        "retention_cutoff": cutoff.isoformat(),
        "high_water_percent": high_percent,
        "low_water_percent": low_percent,
        "high_water_bytes": high_bytes,
        "low_water_bytes": low_bytes,
        "watermark_triggered": watermark_triggered,
        "disk_total_bytes": before.total,
        "disk_used_bytes_before": before.used,
        "disk_free_bytes_before": before.free,
        "disk_used_bytes_after": after.used,
        "disk_free_bytes_after": after.free,
        "projected_used_bytes": projected_used_bytes,
        "cache_bytes_before": cache_bytes_before,
        "cache_bytes_after": max(0, cache_bytes_before - released_bytes),
        "scanned_files": len(scan.files),
        "eligible_files": len(eligible),
        "eligible_bytes": sum(item.size for item in eligible),
        "selected_files": len(selected),
        "selected_bytes": selected_bytes,
        "deleted_files": len(selected) if apply else 0,
        "released_bytes": released_bytes,
        "empty_directories_removed": removed_directories,
        "skipped_files": sum(skipped.values()),
        "skipped_reasons": dict(sorted(skipped.items())),
        "low_water_reached": (
            after.used <= low_bytes
            if apply
            else projected_used_bytes <= low_bytes
        ),
        "candidates": [
            {
                "relative_path": item.relative_path,
                "size": item.size,
                "mtime": item.mtime.isoformat(),
            }
            for item in selected
        ],
    }
    return result

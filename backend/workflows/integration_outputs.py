from __future__ import annotations

import fcntl
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .directory_identity import (
    DirectoryIdentityChangedError,
    DirectoryIdentityLimitError,
    scan_directory_identity,
)
from .models import AnalysisRun, AnalysisRunEvent


class ResourceSnapshotBudgetError(ValueError):
    pass


class DirectorySnapshotLimitError(ResourceSnapshotBudgetError):
    pass


class ResourceSnapshotChangedError(ValueError):
    pass


class GzipProbeLineLimitError(ValueError):
    pass


_DIRECTORY_MANIFEST_PROCESS_SLOT = threading.BoundedSemaphore(1)


def _valid_directory_identity(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(field), int)
        and not isinstance(value.get(field), bool)
        for field in ("mtime_ns", "ctime_ns", "device", "inode")
    )


class OutputSnapshotBudgetError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class ResourceSnapshotBudget:
    """Share one bounded directory-snapshot budget across an HTTP request."""

    def __init__(
        self,
        *,
        deadline_seconds: float | None = None,
        max_resources: int | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        timeout = (
            settings.ANALYSIS_RESOURCE_MANIFEST_TIMEOUT_SECONDS
            if deadline_seconds is None
            else deadline_seconds
        )
        self.deadline = time.monotonic() + max(0.0, float(timeout))
        self.max_resources = max(
            1,
            int(
                settings.ANALYSIS_MANAGED_RESOURCE_MAX_ITEMS
                if max_resources is None
                else max_resources
            ),
        )
        self.resource_count = 0
        self._claimed_resources: set[str] = set()
        self._directory_cache: dict[str, dict[str, Any]] = {}
        self._file_cache: dict[tuple[Any, ...], str] = {}
        self._checkpoint = checkpoint

    def _remaining_seconds(self) -> float:
        if self._checkpoint is not None:
            self._checkpoint()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ResourceSnapshotBudgetError("资源快照超过请求时间上限。")
        return remaining

    def claim_item(self) -> None:
        self._remaining_seconds()
        self.resource_count += 1
        if self.resource_count > self.max_resources:
            raise ResourceSnapshotBudgetError(
                f"受管资源数量超过安全上限（{self.max_resources}）。"
            )

    def claim_unique(self, key: str) -> None:
        self._remaining_seconds()
        if key in self._claimed_resources:
            return
        self.claim_item()
        self._claimed_resources.add(key)

    def checkpoint(self) -> None:
        self._remaining_seconds()

    def directory_manifest(
        self,
        path: Path,
        *,
        containment_root: Path | None = None,
    ) -> dict[str, Any]:
        remaining = self._remaining_seconds()
        cache_key = ":".join(
            (
                str(path.absolute()),
                str(containment_root.absolute()) if containment_root else "",
            )
        )
        cached = self._directory_cache.get(cache_key)
        if cached is not None:
            return cached
        manifest = _directory_manifest_isolated(
            path,
            timeout_seconds=remaining,
            containment_root=containment_root,
        )
        self._remaining_seconds()
        self._directory_cache[cache_key] = manifest
        return manifest

    def file_digest(
        self,
        path: Path,
        *,
        expected_identity: dict[str, int] | None = None,
        containment_root: Path | None = None,
    ) -> str:
        """Hash one stable regular-file descriptor within the request budget."""

        self._remaining_seconds()
        max_bytes = max(
            1,
            int(
                getattr(
                    settings,
                    "ANALYSIS_MANAGED_FILE_CHECKSUM_MAX_BYTES",
                    16 * 1024 * 1024 * 1024,
                )
            ),
        )
        with _open_regular_readonly(
            path,
            containment_root=containment_root,
        ) as handle:
            before = os.fstat(handle.fileno())
            identity = _file_identity(before)
            if expected_identity is not None and any(
                identity.get(field) != expected
                for field, expected in expected_identity.items()
            ):
                raise ResourceSnapshotChangedError(
                    f"资源在内容校验前发生变化：{path.name}"
                )
            if before.st_size > max_bytes:
                raise ResourceSnapshotBudgetError(
                    f"文件内容校验超过安全上限（{max_bytes} 字节）。"
                )
            cache_key = (
                str(path),
                str(containment_root.absolute()) if containment_root else "",
                identity["size"],
                identity["mtime_ns"],
                identity["ctime_ns"],
                identity["device"],
                identity["inode"],
            )
            cached = self._file_cache.get(cache_key)
            if cached is not None:
                with _open_regular_readonly(
                    path,
                    containment_root=containment_root,
                ) as current_handle:
                    current = os.fstat(current_handle.fileno())
                if identity != _file_identity(current):
                    raise ResourceSnapshotChangedError(
                        f"资源在内容校验期间发生变化：{path.name}"
                    )
                return cached

            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                self._remaining_seconds()
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ResourceSnapshotChangedError(
                        f"资源在内容校验期间发生变化：{path.name}"
                    )
                digest.update(chunk)
                remaining -= len(chunk)
            self._remaining_seconds()
            if handle.read(1):
                raise ResourceSnapshotChangedError(
                    f"资源在内容校验期间发生变化：{path.name}"
                )
            after = os.fstat(handle.fileno())
            with _open_regular_readonly(
                path,
                containment_root=containment_root,
            ) as current_handle:
                current = os.fstat(current_handle.fileno())
        if identity != _file_identity(after) or identity != _file_identity(current):
            raise ResourceSnapshotChangedError(
                f"资源在内容校验期间发生变化：{path.name}"
            )
        value = "sha256:" + digest.hexdigest()
        self._file_cache[cache_key] = value
        return value

    def file_identity(
        self,
        path: Path,
        *,
        containment_root: Path,
    ) -> dict[str, int]:
        self._remaining_seconds()
        with _open_regular_readonly(
            path,
            containment_root=containment_root,
        ) as handle:
            identity = _file_identity(os.fstat(handle.fileno()))
        self._remaining_seconds()
        return identity


def _release_directory_process_when_exited(process: subprocess.Popen[str]) -> None:
    try:
        process.wait()
    finally:
        _DIRECTORY_MANIFEST_PROCESS_SLOT.release()


def _directory_manifest_isolated(
    path: Path,
    *,
    timeout_seconds: float,
    containment_root: Path | None = None,
) -> dict[str, Any]:
    """Run request-side NAS traversal in one killable, concurrency-bounded process."""

    if not _DIRECTORY_MANIFEST_PROCESS_SLOT.acquire(blocking=False):
        raise ResourceSnapshotBudgetError(
            "目录快照校验正忙，请稍后重试。"
        )
    release_slot = True
    process: subprocess.Popen[str] | None = None
    try:
        worker = Path(__file__).with_name("directory_manifest_worker.py")
        command = [
                sys.executable,
                str(worker),
                str(path),
                "--max-entries",
                str(
                    max(
                        1,
                        int(
                            getattr(
                                settings,
                                "ANALYSIS_RESOURCE_MANIFEST_MAX_ENTRIES",
                                100_000,
                            )
                        ),
                    )
                ),
                "--max-depth",
                str(
                    max(
                        1,
                        int(
                            getattr(
                                settings,
                                "ANALYSIS_RESOURCE_MANIFEST_MAX_DEPTH",
                                128,
                            )
                        ),
                    )
                ),
                "--deadline-seconds",
                str(max(0.001, timeout_seconds)),
            ]
        if containment_root is not None:
            command.extend(("--containment-root", str(containment_root)))
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
            start_new_session=True,
        )
        try:
            stdout, _ = process.communicate(timeout=max(0.001, timeout_seconds))
        except subprocess.TimeoutExpired as error:
            try:
                process.kill()
            except OSError:
                pass
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            reaper = threading.Thread(
                target=_release_directory_process_when_exited,
                args=(process,),
                name="directory-manifest-reaper",
                daemon=True,
            )
            reaper.start()
            release_slot = False
            raise DirectorySnapshotLimitError(
                f"目录快照超过时间上限：{path}"
            ) from error
        if len(stdout) > 65_536:
            raise ValueError("目录快照子进程返回过多数据。")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ValueError("目录快照子进程返回无效数据。") from error
        if not isinstance(payload, dict) or not payload.get("ok"):
            kind = str(payload.get("kind") or "") if isinstance(payload, dict) else ""
            message = (
                str(payload.get("message") or "目录快照失败。")
                if isinstance(payload, dict)
                else "目录快照失败。"
            )
            if kind == "limit":
                raise DirectorySnapshotLimitError(message)
            if kind == "changed":
                raise ResourceSnapshotChangedError(message)
            raise ValueError(message)
        manifest = payload.get("manifest")
        if (
            not isinstance(manifest, dict)
            or manifest.get("algorithm") != "sha256-tree-identity-v1"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest.get("digest") or ""))
            or not isinstance(manifest.get("entry_count"), int)
            or isinstance(manifest.get("entry_count"), bool)
            or manifest["entry_count"] < 0
            or not _valid_directory_identity(manifest.get("identity"))
        ):
            raise ValueError("目录快照子进程返回无效清单。")
        return manifest
    finally:
        if release_slot:
            _DIRECTORY_MANIFEST_PROCESS_SLOT.release()


class OutputSnapshotBudget:
    """Bound completion-side snapshot work and reuse identical source identities."""

    def __init__(self, *, checkpoint: Callable[[], None] | None = None) -> None:
        self.max_items = max(
            1, int(getattr(settings, "ANALYSIS_OUTPUT_SNAPSHOT_MAX_ITEMS", 256))
        )
        self.max_bytes = max(
            1,
            int(
                getattr(
                    settings,
                    "ANALYSIS_OUTPUT_SNAPSHOT_MAX_BYTES",
                    1024 * 1024 * 1024 * 1024,
                )
            ),
        )
        self.max_directory_entries = max(
            1,
            int(
                getattr(
                    settings,
                    "ANALYSIS_OUTPUT_SNAPSHOT_MAX_DIRECTORY_ENTRIES",
                    200_000,
                )
            ),
        )
        self.max_value_bytes = max(
            1,
            int(
                getattr(
                    settings,
                    "ANALYSIS_OUTPUT_VALUE_MAX_BYTES",
                    65_536,
                )
            ),
        )
        timeout = max(
            0.1,
            float(
                getattr(
                    settings,
                    "ANALYSIS_OUTPUT_SNAPSHOT_TIMEOUT_SECONDS",
                    300,
                )
            ),
        )
        self.deadline = time.monotonic() + timeout
        self.item_count = 0
        self.byte_count = 0
        self.directory_entry_count = 0
        self._source_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._directory_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._claimed_sources: set[tuple[Any, ...]] = set()
        self._checkpoint = checkpoint

    def checkpoint(self) -> None:
        if self._checkpoint is not None:
            self._checkpoint()
        if time.monotonic() >= self.deadline:
            raise OutputSnapshotBudgetError(
                "output_snapshot_timeout_exceeded",
                "输出快照超过总时间上限。",
            )

    def claim_item(self) -> None:
        self.checkpoint()
        self.item_count += 1
        if self.item_count > self.max_items:
            raise OutputSnapshotBudgetError(
                "output_snapshot_item_limit_exceeded",
                f"输出快照项数超过安全上限（{self.max_items}）。"
            )

    def claim_directory_entry(self) -> None:
        self.checkpoint()
        self.directory_entry_count += 1
        if self.directory_entry_count > self.max_directory_entries:
            raise OutputSnapshotBudgetError(
                "output_snapshot_directory_entry_limit_exceeded",
                "输出目录扫描条目总数超过安全上限"
                f"（{self.max_directory_entries}）。",
            )

    @staticmethod
    def source_key(path: Path, identity: dict[str, int]) -> tuple[Any, ...]:
        return (
            str(path),
            identity["size"],
            identity["mtime_ns"],
            identity["ctime_ns"],
            identity["device"],
            identity["inode"],
        )

    def cached(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        return self._source_cache.get(key)

    def cached_directory(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        return self._directory_cache.get(key)

    def claim_source(self, key: tuple[Any, ...], size: int) -> None:
        self.checkpoint()
        if key in self._claimed_sources:
            return
        if self.byte_count + size > self.max_bytes:
            raise OutputSnapshotBudgetError(
                "output_snapshot_byte_limit_exceeded",
                f"输出快照总字节数超过安全上限（{self.max_bytes}）。"
            )
        self.byte_count += size
        self._claimed_sources.add(key)

    def remember(self, key: tuple[Any, ...], snapshot: dict[str, Any]) -> None:
        self._source_cache[key] = snapshot

    def remember_directory(
        self,
        key: tuple[Any, ...],
        manifest: dict[str, Any],
    ) -> None:
        self._directory_cache[key] = manifest


def _local_run_path(value: str | Path) -> Path:
    execution_root = Path(settings.ANALYSIS_RUN_EXECUTION_ROOT).resolve()
    local_root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    resolved = Path(value).resolve()
    try:
        relative = resolved.relative_to(execution_root)
    except ValueError:
        relative = resolved.relative_to(local_root)
    local = (local_root / relative).resolve()
    local.relative_to(local_root)
    return local


def _open_regular_readonly(
    path: Path,
    *,
    containment_root: Path | None = None,
) -> BinaryIO:
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    path_only = getattr(os, "O_PATH", 0)
    guard_flags = (path_only or os.O_RDONLY) | nofollow | nonblocking
    opened_directories: list[int] = []
    if containment_root is not None:
        root = containment_root.absolute()
        relative = path.absolute().relative_to(root)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("受管文件路径无效。")
        directory_flags = (
            (path_only or os.O_RDONLY)
            | os.O_DIRECTORY
            | nofollow
            | nonblocking
        )
        current = os.open(root, directory_flags)
        opened_directories.append(current)
        try:
            for component in relative.parts[:-1]:
                current = os.open(component, directory_flags, dir_fd=current)
                opened_directories.append(current)
            guard = os.open(relative.parts[-1], guard_flags, dir_fd=current)
        except Exception:
            for directory in reversed(opened_directories):
                os.close(directory)
            raise
    else:
        guard = os.open(path, guard_flags)
    try:
        guarded_stat = os.fstat(guard)
        if not stat_module.S_ISREG(guarded_stat.st_mode):
            raise ValueError("路径不是普通文件。")
        if path_only:
            descriptor = os.open(f"/proc/self/fd/{guard}", os.O_RDONLY | nonblocking)
            if _file_identity(os.fstat(descriptor)) != _file_identity(guarded_stat):
                os.close(descriptor)
                raise ValueError("文件在打开期间发生变化。")
        else:
            descriptor = os.dup(guard)
    finally:
        os.close(guard)
        for directory in reversed(opened_directories):
            os.close(directory)
    handle = os.fdopen(descriptor, "rb")
    try:
        if not stat_module.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("路径不是普通文件。")
    except Exception:
        handle.close()
        raise
    return handle


def _validate_gzip_header(
    handle: BinaryIO,
    *,
    checkpoint: Callable[[], None] | None = None,
    max_bytes: int | None = None,
) -> int:
    """Validate a bounded first-member header and return its end offset."""

    limit = max(
        10,
        int(
            getattr(settings, "ANALYSIS_INPUT_GZIP_HEADER_MAX_BYTES", 65_536)
            if max_bytes is None
            else max_bytes
        ),
    )
    consumed = 0

    def read_exact(size: int) -> bytes:
        nonlocal consumed
        if checkpoint is not None:
            checkpoint()
        if size < 0 or consumed + size > limit:
            raise ValueError("gzip header 超过安全上限。")
        value = handle.read(size)
        consumed += len(value)
        if len(value) != size:
            raise ValueError("gzip header 不完整。")
        return value

    def read_terminated() -> None:
        nonlocal consumed
        while consumed < limit:
            if checkpoint is not None and consumed % 1024 == 0:
                checkpoint()
            value = handle.read(1)
            if not value:
                raise ValueError("gzip header 字符串未终止。")
            consumed += 1
            if value == b"\x00":
                return
        raise ValueError("gzip header 超过安全上限。")

    handle.seek(0)
    fixed = read_exact(10)
    if fixed[:3] != b"\x1f\x8b\x08" or fixed[3] & 0xE0:
        raise ValueError("gzip header 无效。")
    flags = fixed[3]
    if flags & 0x04:
        extra_length = int.from_bytes(read_exact(2), "little")
        read_exact(extra_length)
    if flags & 0x08:
        read_terminated()
    if flags & 0x10:
        read_terminated()
    if flags & 0x02:
        read_exact(2)
    header_end = handle.tell()
    handle.seek(0)
    return header_end


def _read_gzip_text_lines(
    handle: BinaryIO,
    *,
    line_count: int,
    max_chars: int,
    encoding: str,
    checkpoint: Callable[[], None] | None = None,
) -> list[str]:
    """Read bounded text lines from the first gzip member only.

    The standard gzip reader transparently crosses concatenated members and can
    spend unbounded time parsing a later optional header.  This probe starts at
    the already bounded first header, limits compressed and decompressed work,
    and rejects a first member that does not contain the requested record.
    """

    if line_count < 1 or max_chars < 1:
        raise ValueError("gzip 探测参数无效。")
    header_end = _validate_gzip_header(
        handle,
        checkpoint=checkpoint,
    )
    handle.seek(header_end)
    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    max_line_bytes = max_chars * 4 + 2
    max_output_bytes = line_count * max_line_bytes
    max_compressed_bytes = max_output_bytes + 65_536
    compressed_bytes = 0
    output_bytes = 0
    pending = bytearray()
    lines: list[str] = []

    def consume(value: bytes) -> None:
        nonlocal output_bytes, pending
        output_bytes += len(value)
        if output_bytes > max_output_bytes:
            raise GzipProbeLineLimitError("gzip 文本记录超过安全上限。")
        pending.extend(value)
        while len(lines) < line_count:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(pending[:newline])
            del pending[: newline + 1]
            if len(raw_line) > max_line_bytes:
                raise GzipProbeLineLimitError("gzip 文本记录超过安全上限。")
            line = raw_line.decode(encoding, errors="strict").rstrip("\r")
            if len(line) > max_chars:
                raise GzipProbeLineLimitError("gzip 文本记录超过安全上限。")
            lines.append(line)
        if len(pending) > max_line_bytes:
            raise GzipProbeLineLimitError("gzip 文本记录超过安全上限。")

    while len(lines) < line_count:
        if checkpoint is not None:
            checkpoint()
        remaining_compressed = max_compressed_bytes - compressed_bytes
        if remaining_compressed <= 0:
            raise ValueError("gzip 内容探测超过安全上限。")
        chunk = handle.read(min(65_536, remaining_compressed))
        compressed_bytes += len(chunk)
        if checkpoint is not None:
            checkpoint()
        if not chunk:
            raise ValueError("gzip 首个 member 不完整。")
        compressed = chunk
        while compressed:
            remaining_output = max_output_bytes - output_bytes
            try:
                value = decoder.decompress(compressed, remaining_output + 1)
            except zlib.error as error:
                raise ValueError("gzip deflate 内容无效。") from error
            consume(value)
            if len(lines) >= line_count:
                return lines
            compressed = decoder.unconsumed_tail
            if decoder.eof:
                if pending:
                    raw_line = bytes(pending)
                    pending.clear()
                    line = raw_line.decode(encoding, errors="strict").rstrip("\r")
                    if len(line) > max_chars:
                        raise GzipProbeLineLimitError(
                            "gzip 文本记录超过安全上限。"
                        )
                    lines.append(line)
                if len(lines) >= line_count:
                    return lines
                raise ValueError("gzip 首个 member 不包含完整记录。")
            if not compressed:
                break
    return lines


def _sha256(
    path: Path,
    *,
    checkpoint: Callable[[], None] | None = None,
    max_bytes: int | None = None,
    containment_root: Path | None = None,
) -> str:
    with _open_regular_readonly(
        path,
        containment_root=containment_root,
    ) as handle:
        before = os.fstat(handle.fileno())
        if max_bytes is not None and before.st_size > max_bytes:
            raise ResourceSnapshotBudgetError(
                f"文件内容校验超过安全上限（{max_bytes} 字节）。"
            )
        identity = _file_identity(before)
        digest = _sha256_handle(
            handle,
            checkpoint=checkpoint,
            expected_size=before.st_size,
        )
        after = os.fstat(handle.fileno())
        with _open_regular_readonly(
            path,
            containment_root=containment_root,
        ) as current_handle:
            current = os.fstat(current_handle.fileno())
    if identity != _file_identity(after) or identity != _file_identity(current):
        raise ResourceSnapshotChangedError(
            f"文件在内容校验期间发生变化：{path.name}"
        )
    return digest


def _sha256_handle(
    handle: BinaryIO,
    *,
    checkpoint: Callable[[], None] | None = None,
    expected_size: int | None = None,
) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    remaining = expected_size
    while remaining is None or remaining > 0:
        if checkpoint is not None:
            checkpoint()
        read_size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
        chunk = handle.read(read_size)
        if not chunk:
            if remaining:
                raise ValueError("文件在摘要计算期间被截断。")
            break
        digest.update(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    if expected_size is not None and handle.read(1):
        raise ValueError("文件在摘要计算期间增长。")
    return "sha256:" + digest.hexdigest()


def _file_identity(stat: os.stat_result) -> dict[str, int]:
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _snapshot_output_file(
    run_root: Path,
    path: Path,
    *,
    snapshot_budget: OutputSnapshotBudget,
) -> dict[str, Any]:
    snapshot_root = run_root / ".verified-outputs"
    if snapshot_root.is_symlink():
        raise ValueError("输出快照目录不能是符号链接。")
    snapshot_root.mkdir(mode=0o750, exist_ok=True)
    snapshot_root = snapshot_root.resolve()
    snapshot_root.relative_to(run_root)

    temporary_path: Path | None = None
    temporary_handle: BinaryIO | None = None
    try:
        digest = hashlib.sha256()
        with _open_regular_readonly(path) as source:
            before = os.fstat(source.fileno())
            if not stat_module.S_ISREG(before.st_mode):
                raise ValueError("输出不是普通文件。")
            source_identity = _file_identity(before)
            source_key = snapshot_budget.source_key(path, source_identity)
            cached = snapshot_budget.cached(source_key)
            if cached is not None:
                after = os.fstat(source.fileno())
                current = os.stat(path, follow_symlinks=False)
                if (
                    source_identity != _file_identity(after)
                    or source_identity != _file_identity(current)
                ):
                    raise ValueError("输出文件在固化清单期间发生变化。")
                return cached
            snapshot_budget.claim_source(source_key, before.st_size)
            minimum_free = max(
                0,
                int(
                    getattr(
                        settings,
                        "ANALYSIS_OUTPUT_SNAPSHOT_MIN_FREE_BYTES",
                        1024 * 1024 * 1024,
                    )
                ),
            )
            if shutil.disk_usage(snapshot_root).free < before.st_size + minimum_free:
                raise OutputSnapshotBudgetError(
                    "output_snapshot_storage_insufficient",
                    "输出快照存储空间不足。",
                )
            temporary_handle = tempfile.NamedTemporaryFile(
                mode="w+b",
                dir=snapshot_root,
                prefix=".snapshot-",
                delete=False,
            )
            temporary_path = Path(temporary_handle.name)
            remaining = before.st_size
            while remaining:
                snapshot_budget.checkpoint()
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("输出文件在固化清单期间被截断。")
                digest.update(chunk)
                temporary_handle.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ValueError("输出文件在固化清单期间增长。")
            temporary_handle.flush()
            os.fsync(temporary_handle.fileno())
            os.fchmod(temporary_handle.fileno(), 0o444)
            after = os.fstat(source.fileno())
            current = os.stat(path, follow_symlinks=False)
        snapshot_budget.checkpoint()
        if (
            source_identity != _file_identity(after)
            or source_identity != _file_identity(current)
        ):
            raise ValueError("输出文件在固化清单期间发生变化。")

        sha256 = "sha256:" + digest.hexdigest()
        snapshot_before = os.fstat(temporary_handle.fileno())
        actual_sha256 = _sha256_handle(
            temporary_handle,
            checkpoint=snapshot_budget.checkpoint,
            expected_size=source_identity["size"],
        )
        snapshot_after = os.fstat(temporary_handle.fileno())
        if (
            _file_identity(snapshot_before) != _file_identity(snapshot_after)
            or actual_sha256 != sha256
        ):
            raise ValueError("输出快照与内容地址不一致。")

        snapshot_path = snapshot_root / digest.hexdigest()
        lock_flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        lock_descriptor = os.open(
            snapshot_root / ".publish.lock",
            lock_flags,
            0o600,
        )
        created = False
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            try:
                os.link(temporary_path, snapshot_path)
                created = True
            except FileExistsError:
                pass
            temporary_path.unlink()
            temporary_path = None
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(snapshot_root, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            if created:
                target_stat = os.stat(snapshot_path, follow_symlinks=False)
                if (
                    snapshot_after.st_size != target_stat.st_size
                    or snapshot_after.st_mtime_ns != target_stat.st_mtime_ns
                    or snapshot_after.st_dev != target_stat.st_dev
                    or snapshot_after.st_ino != target_stat.st_ino
                ):
                    raise ValueError("输出快照与内容地址不一致。")
                snapshot_after = target_stat
            else:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                descriptor = os.open(snapshot_path, flags)
                with os.fdopen(descriptor, "rb") as snapshot_handle:
                    snapshot_before = os.fstat(snapshot_handle.fileno())
                    if (
                        not stat_module.S_ISREG(snapshot_before.st_mode)
                        or snapshot_before.st_mode & 0o222
                        or snapshot_before.st_size != source_identity["size"]
                    ):
                        raise ValueError("输出快照目标无效。")
                    actual_sha256 = _sha256_handle(
                        snapshot_handle,
                        checkpoint=snapshot_budget.checkpoint,
                        expected_size=source_identity["size"],
                    )
                    snapshot_after = os.fstat(snapshot_handle.fileno())
                if (
                    _file_identity(snapshot_before) != _file_identity(snapshot_after)
                    or actual_sha256 != sha256
                ):
                    raise ValueError("输出快照与内容地址不一致。")
            if (
                not stat_module.S_ISREG(snapshot_after.st_mode)
                or snapshot_after.st_mode & 0o222
            ):
                raise ValueError("输出快照目标无效。")
            snapshot = {
                "path": str(snapshot_path),
                "sha256": sha256,
                "identity": _file_identity(snapshot_after),
                "source_path": str(path),
                "source_identity": _file_identity(after),
            }
            snapshot_budget.remember(source_key, snapshot)
            return snapshot
        except Exception:
            if created:
                try:
                    snapshot_path.unlink()
                except OSError:
                    pass
                try:
                    directory_descriptor = os.open(
                        snapshot_root,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                    try:
                        os.fsync(directory_descriptor)
                    finally:
                        os.close(directory_descriptor)
                except OSError:
                    pass
            raise
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
    finally:
        if temporary_handle is not None:
            temporary_handle.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _directory_manifest(
    path: Path,
    *,
    deadline_seconds: float | None = None,
    checkpoint: Callable[[], None] | None = None,
    claim_entry: Callable[[], None] | None = None,
    max_entries: int | None = None,
    max_depth: int | None = None,
) -> dict[str, Any]:
    entry_limit = max(
        1,
        int(
            getattr(settings, "ANALYSIS_RESOURCE_MANIFEST_MAX_ENTRIES", 100_000)
            if max_entries is None
            else max_entries
        ),
    )
    depth_limit = max(
        1,
        int(
            getattr(settings, "ANALYSIS_RESOURCE_MANIFEST_MAX_DEPTH", 128)
            if max_depth is None
            else max_depth
        ),
    )
    try:
        return scan_directory_identity(
            path,
            deadline_seconds=deadline_seconds,
            checkpoint=checkpoint,
            claim_entry=claim_entry,
            max_entries=entry_limit,
            max_depth=depth_limit,
        )
    except DirectoryIdentityLimitError as error:
        raise DirectorySnapshotLimitError(str(error)) from error
    except DirectoryIdentityChangedError as error:
        raise ResourceSnapshotChangedError(str(error)) from error


def open_verified_output(
    item: dict[str, Any],
    *,
    run_root: str | Path,
) -> tuple[Path, BinaryIO]:
    """Open an immutable output and verify its POSIX identity on the response FD."""

    root = _local_run_path(run_root)
    source_path = _local_run_path(str(item["source_path"]))
    source_path.relative_to(root)
    source_identity = item.get("source_identity")
    if not isinstance(source_identity, dict):
        raise ValueError("输出缺少源文件完整性信息。")
    with _open_regular_readonly(source_path) as source:
        source_stat = os.fstat(source.fileno())
        if not stat_module.S_ISREG(source_stat.st_mode):
            raise ValueError("输出源不是普通文件。")
        if _file_identity(source_stat) != source_identity:
            raise ValueError("输出源文件身份已变化。")

    path = _local_run_path(str(item["path"]))
    snapshot_root = (root / ".verified-outputs").resolve()
    path.relative_to(snapshot_root)
    expected_sha256 = str(item.get("sha256") or "")
    expected_name = expected_sha256.removeprefix("sha256:")
    if not expected_name or path.parent != snapshot_root or path.name != expected_name:
        raise ValueError("输出快照路径与内容地址不一致。")
    handle = _open_regular_readonly(path)
    try:
        file_stat = os.fstat(handle.fileno())
        if not stat_module.S_ISREG(file_stat.st_mode):
            raise ValueError("输出不是普通文件。")
        identity = item.get("identity")
        if not isinstance(identity, dict) or not expected_sha256:
            raise ValueError("输出缺少完整性信息。")
        expected_identity = (
            identity.get("size"),
            identity.get("mtime_ns"),
            identity.get("ctime_ns"),
            identity.get("device"),
            identity.get("inode"),
        )
        actual_identity = (
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
            file_stat.st_dev,
            file_stat.st_ino,
        )
        if actual_identity != expected_identity:
            raise ValueError("输出文件身份已变化。")
        handle.seek(0)
    except Exception:
        handle.close()
        raise
    return path, handle


def _public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _public_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if not isinstance(value, str):
        return value
    roots = {
        str(getattr(settings, name, "") or "").rstrip("/")
        for name in (
            "ANALYSIS_RAWDATA_ROOT",
            "ANALYSIS_RAWDATA_EXECUTION_ROOT",
            "ANALYSIS_DATABASE_ROOT",
            "ANALYSIS_DATABASE_EXECUTION_ROOT",
            "ANALYSIS_RUN_ROOT",
            "ANALYSIS_RUN_EXECUTION_ROOT",
        )
    }
    result = value
    for root in sorted((item for item in roots if item), key=len, reverse=True):
        result = result.replace(root, "<managed-root>")
    return result


def _split_pair_type(value: str) -> tuple[str, str] | None:
    if not value.startswith("Pair[") or not value.endswith("]"):
        return None
    inner = value[5:-1]
    depth = 0
    for index, character in enumerate(inner):
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "," and depth == 0:
            return inner[:index].strip(), inner[index + 1 :].strip()
    return None


def output_value_limit_reason(value: Any) -> str | None:
    """Validate an inline JSON value without serializing an unbounded copy."""

    max_bytes = max(
        1,
        int(getattr(settings, "ANALYSIS_OUTPUT_VALUE_MAX_BYTES", 65_536)),
    )
    max_depth = max(
        1,
        int(getattr(settings, "ANALYSIS_OUTPUT_MANIFEST_MAX_DEPTH", 32)),
    )
    used = 0

    def add_size(size: int) -> bool:
        nonlocal used
        used += size
        return used <= max_bytes

    def visit(item: Any, depth: int) -> str | None:
        if depth > max_depth:
            return "output_value_depth_exceeded"
        if item is None:
            return None if add_size(4) else "output_value_size_exceeded"
        if isinstance(item, bool):
            return None if add_size(4 if item else 5) else "output_value_size_exceeded"
        if isinstance(item, float) and not math.isfinite(item):
            return "output_value_type_invalid"
        if isinstance(item, (int, float)):
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            return None if add_size(len(encoded)) else "output_value_size_exceeded"
        if isinstance(item, str):
            if len(item) > max_bytes - used:
                return "output_value_size_exceeded"
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            return (
                None
                if add_size(len(encoded.encode("utf-8")))
                else "output_value_size_exceeded"
            )
        if isinstance(item, list):
            if not add_size(2):
                return "output_value_size_exceeded"
            for index, child in enumerate(item):
                if index and not add_size(1):
                    return "output_value_size_exceeded"
                reason = visit(child, depth + 1)
                if reason is not None:
                    return reason
            return None
        if isinstance(item, dict):
            if not add_size(2):
                return "output_value_size_exceeded"
            for index, (key, child) in enumerate(item.items()):
                if not isinstance(key, str):
                    return "output_value_type_invalid"
                if index and not add_size(1):
                    return "output_value_size_exceeded"
                reason = visit(key, depth + 1)
                if reason is not None:
                    return reason
                if not add_size(1):
                    return "output_value_size_exceeded"
                reason = visit(child, depth + 1)
                if reason is not None:
                    return reason
            return None
        return "output_value_type_invalid"

    return visit(value, 0)


def _manifest_value(
    run_root: Path,
    *,
    key: str,
    value: Any,
    wdl_type: str,
    contract: dict[str, Any],
    snapshot_budget: OutputSnapshotBudget,
    depth: int = 0,
    claim: bool = True,
) -> list[dict[str, Any]]:
    base = {
        "key": key,
        "name": contract.get("name") or key,
        "label": contract.get("label") or contract.get("name") or key,
        "semantic_type": contract.get("semantic_type") or "core.output.unknown",
        "wdl_type": wdl_type,
        "required": bool(contract.get("required", False)),
    }
    if claim:
        try:
            snapshot_budget.claim_item()
        except OutputSnapshotBudgetError as error:
            return [{**base, "kind": "unverifiable", "reason": error.reason}]
    optional_type = wdl_type.removesuffix("?")
    if value is None:
        if wdl_type.endswith("?"):
            return []
        return [{**base, "kind": "unverifiable", "reason": "output_type_invalid"}]
    if depth > int(getattr(settings, "ANALYSIS_OUTPUT_MANIFEST_MAX_DEPTH", 32)):
        return [
            {
                "key": key,
                "name": contract.get("name") or key,
                "label": contract.get("label") or contract.get("name") or key,
                "semantic_type": contract.get("semantic_type")
                or "core.output.unknown",
                "wdl_type": wdl_type,
                "required": bool(contract.get("required", False)),
                "kind": "unverifiable",
                "reason": "output_manifest_depth_exceeded",
            }
        ]
    if optional_type.startswith("Array[") and optional_type.endswith("]"):
        subtype = optional_type[6:-1].strip()
        if not isinstance(value, list):
            return [
                {**base, "kind": "unverifiable", "reason": "output_type_invalid"}
            ]
        if not value:
            return [{**base, "kind": "value", "value": []}]
        items: list[dict[str, Any]] = []
        for index, item in enumerate(value):
            items.extend(
                _manifest_value(
                    run_root,
                    key=f"{key}[{index}]",
                    value=item,
                    wdl_type=subtype,
                    contract=contract,
                    snapshot_budget=snapshot_budget,
                    depth=depth + 1,
                )
            )
            if items and items[-1].get("reason") in {
                "output_snapshot_item_limit_exceeded",
                "output_manifest_depth_exceeded",
            }:
                break
        return items
    pair = _split_pair_type(optional_type)
    if pair:
        if not isinstance(value, dict) or not all(
            side in value for side in ("left", "right")
        ):
            return [
                {**base, "kind": "unverifiable", "reason": "output_type_invalid"}
            ]
        items = []
        for side, subtype in zip(("left", "right"), pair, strict=True):
            items.extend(
                _manifest_value(
                    run_root,
                    key=f"{key}.{side}",
                    value=value.get(side),
                    wdl_type=subtype,
                    contract=contract,
                    snapshot_budget=snapshot_budget,
                    depth=depth + 1,
                )
            )
        return items

    if optional_type not in {"File", "Directory"}:
        reason = output_value_limit_reason(value)
        if reason is not None:
            return [{**base, "kind": "unverifiable", "reason": reason}]
        return [{**base, "kind": "value", "value": value}]
    if not isinstance(value, str):
        return [
            {**base, "kind": "unverifiable", "reason": "output_type_invalid"}
        ]
    try:
        path = _local_run_path(value)
        path.relative_to(run_root)
    except ValueError:
        return [
            {**base, "kind": "unverifiable", "reason": "output_path_invalid"}
        ]
    try:
        stat = path.stat()
    except OSError:
        return [
            {**base, "kind": "unverifiable", "reason": "output_path_missing"}
        ]
    if optional_type == "Directory":
        if not path.is_dir():
            return [
                {**base, "kind": "unverifiable", "reason": "output_type_invalid"}
            ]
        try:
            snapshot_root = run_root / ".verified-outputs"
            if path.is_relative_to(snapshot_root) or snapshot_root.is_relative_to(path):
                raise OutputSnapshotBudgetError(
                    "output_directory_conflicts_with_snapshot_store",
                    "Directory 输出不能与输出快照目录重叠。"
                )
            directory_key = snapshot_budget.source_key(path, _file_identity(stat))
            directory_manifest = snapshot_budget.cached_directory(directory_key)
            if directory_manifest is None:
                directory_manifest = _directory_manifest(
                    path,
                    checkpoint=snapshot_budget.checkpoint,
                    claim_entry=snapshot_budget.claim_directory_entry,
                )
                confirmed_manifest = _directory_manifest(
                    path,
                    checkpoint=snapshot_budget.checkpoint,
                    claim_entry=snapshot_budget.claim_directory_entry,
                )
                current_stat = path.stat()
                if (
                    confirmed_manifest != directory_manifest
                    or _file_identity(current_stat) != _file_identity(stat)
                ):
                    raise ResourceSnapshotChangedError(
                        "输出目录在固化清单期间发生变化。"
                    )
                snapshot_budget.remember_directory(
                    directory_key,
                    directory_manifest,
                )
            elif _file_identity(path.stat()) != _file_identity(stat):
                raise ResourceSnapshotChangedError(
                    "输出目录在复用固化清单前发生变化。"
                )
        except OutputSnapshotBudgetError as error:
            return [
                {
                    **base,
                    "kind": "unverifiable",
                    "reason": error.reason,
                }
            ]
        except (OSError, ValueError):
            return [
                {
                    **base,
                    "kind": "unverifiable",
                    "reason": "directory_snapshot_failed",
                }
            ]
        return [
            {
                **base,
                "kind": "directory",
                "path": str(path),
                "digest": directory_manifest["digest"],
                "entry_count": directory_manifest["entry_count"],
                "identity": {
                    "mtime_ns": stat.st_mtime_ns,
                    "ctime_ns": stat.st_ctime_ns,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                },
            }
        ]
    if not path.is_file():
        return [
            {**base, "kind": "unverifiable", "reason": "output_type_invalid"}
        ]
    try:
        snapshot = _snapshot_output_file(
            run_root,
            path,
            snapshot_budget=snapshot_budget,
        )
    except OutputSnapshotBudgetError as error:
        return [
            {
                **base,
                "kind": "unverifiable",
                "reason": error.reason,
            }
        ]
    except (OSError, ValueError):
        return [
            {
                **base,
                "kind": "unverifiable",
                "reason": "file_digest_failed",
            }
        ]
    return [
        {
            **base,
            "kind": "file",
            "path": snapshot["path"],
            "source_path": snapshot["source_path"],
            "source_identity": snapshot["source_identity"],
            "filename": path.name,
            "size": snapshot["identity"]["size"],
            "sha256": snapshot["sha256"],
            "content_type": mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
            "identity": snapshot["identity"],
        }
    ]


def _manifest_uncontracted_value(
    run_root: Path,
    *,
    key: str,
    value: Any,
    snapshot_budget: OutputSnapshotBudget,
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Manifest outputs for local runs that have no semantic contract."""

    base = {
        "key": key,
        "name": key,
        "label": key,
        "semantic_type": "core.output.unknown",
        "wdl_type": "String",
        "required": False,
    }
    try:
        snapshot_budget.claim_item()
    except OutputSnapshotBudgetError as error:
        return [{**base, "kind": "unverifiable", "reason": error.reason}]
    if depth > int(getattr(settings, "ANALYSIS_OUTPUT_MANIFEST_MAX_DEPTH", 32)):
        return [
            {
                "key": key,
                "name": key,
                "label": key,
                "semantic_type": "core.output.unknown",
                "wdl_type": "String",
                "required": False,
                "kind": "unverifiable",
                "reason": "output_manifest_depth_exceeded",
            }
        ]
    if value is None:
        return [{**base, "kind": "value", "value": None}]
    if isinstance(value, dict):
        items: list[dict[str, Any]] = []
        for child_key, child_value in value.items():
            nested_key = f"{key}.{child_key}" if key else str(child_key)
            items.extend(
                _manifest_uncontracted_value(
                    run_root,
                    key=nested_key,
                    value=child_value,
                    snapshot_budget=snapshot_budget,
                    depth=depth + 1,
                )
            )
            if items and items[-1].get("reason") in {
                "output_snapshot_item_limit_exceeded",
                "output_manifest_depth_exceeded",
            }:
                break
        return items
    if isinstance(value, list):
        items = []
        for index, child_value in enumerate(value):
            items.extend(
                _manifest_uncontracted_value(
                    run_root,
                    key=f"{key}[{index}]",
                    value=child_value,
                    snapshot_budget=snapshot_budget,
                    depth=depth + 1,
                )
            )
            if items and items[-1].get("reason") in {
                "output_snapshot_item_limit_exceeded",
                "output_manifest_depth_exceeded",
            }:
                break
        return items

    contract = {
        "name": key,
        "label": key,
        "semantic_type": "core.output.unknown",
        "required": False,
    }
    wdl_type = "String"
    if isinstance(value, str):
        candidate = Path(value)
        managed_path_value = False
        if candidate.is_absolute():
            for setting_name in (
                "ANALYSIS_RUN_ROOT",
                "ANALYSIS_RUN_EXECUTION_ROOT",
            ):
                try:
                    candidate.relative_to(Path(getattr(settings, setting_name)))
                    managed_path_value = True
                    break
                except ValueError:
                    continue
        try:
            path = _local_run_path(value)
            path.relative_to(run_root)
        except (OSError, ValueError):
            path = None
            if managed_path_value:
                return [
                    {
                        "key": key,
                        **contract,
                        "wdl_type": "File",
                        "kind": "unverifiable",
                        "reason": "output_path_invalid",
                    }
                ]
        if path is not None:
            if path.is_file():
                wdl_type = "File"
            elif path.is_dir():
                wdl_type = "Directory"
            elif managed_path_value:
                return [
                    {
                        "key": key,
                        **contract,
                        "wdl_type": "File",
                        "kind": "unverifiable",
                        "reason": "output_path_missing",
                    }
                ]
    return _manifest_value(
        run_root,
        key=key,
        value=value,
        wdl_type=wdl_type,
        contract=contract,
        snapshot_budget=snapshot_budget,
        claim=False,
    )


def build_output_manifest(
    run: AnalysisRun,
    result: dict[str, Any],
    *,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    contract = run.request_payload.get("integration_output_contract") or []
    if not isinstance(contract, list):
        contract = []
    values = result.get("outputs", result)
    if not isinstance(values, dict):
        values = {}
    run_root = _local_run_path(run.work_directory)
    snapshot_budget = OutputSnapshotBudget(checkpoint=checkpoint)
    items: list[dict[str, Any]] = []
    consumed: set[str] = set()
    missing_required: list[dict[str, Any]] = []
    missing_required_truncated = False

    for raw_contract in contract:
        if not isinstance(raw_contract, dict):
            continue
        expected_key = str(raw_contract.get("key") or "")
        if not expected_key:
            continue
        try:
            snapshot_budget.claim_item()
        except OutputSnapshotBudgetError as error:
            missing_required_truncated = True
            items.append(
                {
                    "key": "<contract>",
                    "name": "<contract>",
                    "label": "输出契约",
                    "semantic_type": "core.output.unknown",
                    "wdl_type": "Object",
                    "required": False,
                    "kind": "unverifiable",
                    "reason": error.reason,
                }
            )
            break
        if expected_key not in values or values[expected_key] is None:
            if raw_contract.get("required"):
                missing_required.append(
                    {
                        "key": expected_key,
                        "semantic_type": raw_contract.get("semantic_type"),
                        "label": raw_contract.get("label") or raw_contract.get("name"),
                    }
                )
            continue
        consumed.add(expected_key)
        output_items = _manifest_value(
            run_root,
            key=expected_key,
            value=values[expected_key],
            wdl_type=str(raw_contract.get("wdl_type") or "String"),
            contract=raw_contract,
            snapshot_budget=snapshot_budget,
            claim=False,
        )
        items.extend(output_items)
        if items and items[-1].get("reason") in {
            "output_snapshot_item_limit_exceeded",
            "output_manifest_depth_exceeded",
        }:
            break

    if not contract:
        for key, value in values.items():
            key_value = str(key)
            consumed.add(key_value)
            output_items = _manifest_uncontracted_value(
                run_root,
                key=key_value,
                value=value,
                snapshot_budget=snapshot_budget,
            )
            items.extend(output_items)
            if items and items[-1].get("reason") in {
                "output_snapshot_item_limit_exceeded",
                "output_manifest_depth_exceeded",
            }:
                break

    uncontracted_total = len(values) - len(consumed)
    uncontracted_output_keys: list[str] = []
    for key in values:
        if key in consumed:
            continue
        if len(uncontracted_output_keys) >= snapshot_budget.max_items:
            break
        uncontracted_output_keys.append(str(key))
    uncontracted_output_keys.sort()
    uncontracted_truncated = uncontracted_total > len(uncontracted_output_keys)
    if uncontracted_truncated:
        items.append(
            {
                "key": "<uncontracted>",
                "name": "<uncontracted>",
                "label": "未契约输出",
                "semantic_type": "core.output.unknown",
                "wdl_type": "Object",
                "required": False,
                "kind": "unverifiable",
                "reason": "output_manifest_uncontracted_key_limit_exceeded",
            }
        )

    manifest = {
        "schema_version": 1,
        "integrity_version": 2,
        "created_at": timezone.now().isoformat(),
        "items": items,
        "missing_required": missing_required,
        "missing_required_truncated": missing_required_truncated,
        "uncontracted_output_keys": uncontracted_output_keys,
        "uncontracted_output_key_count": uncontracted_total,
        "uncontracted_output_keys_truncated": uncontracted_truncated,
    }
    unverifiable_outputs = [
        {"key": item.get("key"), "reason": item.get("reason")}
        for item in items
        if item.get("kind") == "unverifiable"
    ]
    manifest["unverifiable_outputs"] = unverifiable_outputs
    if missing_required or unverifiable_outputs:
        error_code = (
            "REQUIRED_OUTPUT_MISSING"
            if missing_required
            else "OUTPUT_INTEGRITY_UNVERIFIABLE"
        )
        return (
            manifest,
            AnalysisRun.OutputStatus.INCOMPLETE,
            {
                "code": error_code,
                "category": "application",
                "retryable": False,
                "details": {
                    "missing": missing_required,
                    "unverifiable": unverifiable_outputs,
                },
            },
        )
    return manifest, AnalysisRun.OutputStatus.COMPLETE, None


_OUTPUT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def output_manifest_has_integrity_v2(value: Any) -> bool:
    """Recognize v2 ownership even when the manifest is incomplete or malformed."""

    return (
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and value.get("integrity_version") == 2
    )


def _valid_output_identity(identity: Any, fields: tuple[str, ...]) -> bool:
    return isinstance(identity, dict) and all(
        isinstance(identity.get(field), int)
        and not isinstance(identity.get(field), bool)
        for field in fields
    )


def output_manifest_file_item_is_verified(item: Any) -> bool:
    fields = ("size", "mtime_ns", "ctime_ns", "device", "inode")
    return (
        isinstance(item, dict)
        and item.get("kind") == "file"
        and isinstance(item.get("key"), str)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("source_path"), str)
        and bool(_OUTPUT_DIGEST_PATTERN.fullmatch(str(item.get("sha256") or "")))
        and _valid_output_identity(item.get("identity"), fields)
        and _valid_output_identity(item.get("source_identity"), fields)
    )


def _output_manifest_item_is_valid(item: Any) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("key"), str):
        return False
    kind = item.get("kind")
    if kind == "value":
        return "value" in item and output_value_limit_reason(item["value"]) is None
    if kind == "file":
        return output_manifest_file_item_is_verified(item)
    if kind != "directory":
        return False
    fields = ("mtime_ns", "ctime_ns", "device", "inode")
    return (
        isinstance(item.get("path"), str)
        and bool(_OUTPUT_DIGEST_PATTERN.fullmatch(str(item.get("digest") or "")))
        and _valid_output_identity(item.get("identity"), fields)
        and isinstance(item.get("entry_count"), int)
        and not isinstance(item.get("entry_count"), bool)
        and item["entry_count"] >= 0
    )


def output_manifest_is_current(value: Any) -> bool:
    if (
        not output_manifest_has_integrity_v2(value)
        or bool(value.get("missing_required"))
        or bool(value.get("unverifiable_outputs"))
    ):
        return False
    items = value.get("items")
    return isinstance(items, list) and all(
        _output_manifest_item_is_valid(item) for item in items
    )


def _previous_output_evidence(
    manifest: dict[str, Any],
    *,
    run_root: Path,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    evidence: list[tuple[str, str, str]] = []
    baselined_directories: list[str] = []
    items = manifest.get("items", [])
    if not isinstance(items, list):
        raise ValueError("历史输出清单的 items 结构无效。")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("历史输出清单包含无效条目。")
        kind = str(item.get("kind") or "")
        if kind == "value":
            continue
        if kind not in {"file", "directory"}:
            raise ValueError("历史输出清单包含未知条目类型。")
        key = str(item.get("key") or "")
        if not key:
            raise ValueError("历史输出清单包含缺少 key 的文件条目。")
        path_value = item.get("source_path") or item.get("path")
        expected = str(
            (item.get("sha256") if kind == "file" else item.get("digest")) or ""
        )
        if not path_value:
            raise ValueError(f"历史输出 {key or '<unknown>'} 缺少可验证证据。")
        if kind == "directory" and not expected:
            baselined_directories.append(key)
            continue
        if not expected:
            raise ValueError(f"历史输出 {key or '<unknown>'} 缺少可验证证据。")
        path = _local_run_path(str(path_value))
        path.relative_to(run_root)
        actual = (
            _sha256(path)
            if kind == "file"
            else _directory_manifest(path)["digest"]
        )
        normalized_expected = (
            expected if expected.startswith("sha256:") else f"sha256:{expected}"
        )
        if actual != normalized_expected:
            raise ValueError(f"历史输出 {key or '<unknown>'} 与原完整性证据不一致。")
        evidence.append((key, kind, normalized_expected))
    return evidence, baselined_directories


def _assert_preserved_output_evidence(
    evidence: list[tuple[str, str, str]],
    candidate: dict[str, Any],
) -> None:
    candidate_evidence = {
        (str(item.get("key") or ""), str(item.get("kind") or "")): str(
            (
                item.get("sha256")
                if item.get("kind") == "file"
                else item.get("digest")
            )
            or ""
        )
        for item in candidate.get("items", [])
        if isinstance(item, dict) and item.get("kind") in {"file", "directory"}
    }
    for key, kind, expected in evidence:
        actual = candidate_evidence.get((key, kind), "")
        normalized_actual = (
            actual if actual.startswith("sha256:") else f"sha256:{actual}"
        )
        if normalized_actual != expected:
            raise ValueError(f"升级后输出 {key or '<unknown>'} 未保留原完整性证据。")


def backfill_output_manifest(run: AnalysisRun, *, source: str) -> bool:
    """Upgrade historical output evidence without accepting changed artifacts."""

    if output_manifest_is_current(run.output_manifest):
        if run.output_status == AnalysisRun.OutputStatus.COMPLETE:
            return True
        with transaction.atomic():
            locked = AnalysisRun.objects.select_for_update().get(pk=run.pk)
            if not output_manifest_is_current(locked.output_manifest):
                return False
            if locked.output_status != AnalysisRun.OutputStatus.COMPLETE:
                locked.output_status = AnalysisRun.OutputStatus.COMPLETE
                locked.error = ""
                locked.error_code = ""
                locked.error_category = ""
                locked.error_retryable = False
                locked.error_details = {}
                locked.save(
                    update_fields=[
                        "output_status",
                        "error",
                        "error_code",
                        "error_category",
                        "error_retryable",
                        "error_details",
                        "updated_at",
                    ]
                )
                AnalysisRunEvent.objects.create(
                    run=locked,
                    kind="output_manifest_backfill",
                    message="已根据完整的输出清单修复历史输出状态。",
                    details={"source": source, "repair": "output_status"},
                )
            run.output_status = locked.output_status
            run.error = locked.error
            run.error_code = locked.error_code
            run.error_category = locked.error_category
            run.error_retryable = locked.error_retryable
            run.error_details = locked.error_details
        return run.output_status == AnalysisRun.OutputStatus.COMPLETE
    if run.status != AnalysisRun.Status.SUCCEEDED or not run.work_directory:
        return False
    run_root = _local_run_path(run.work_directory)
    if not run_root.is_dir():
        raise ValueError("历史运行目录不存在或无法读取。")

    original_manifest = (
        run.output_manifest if isinstance(run.output_manifest, dict) else {}
    )
    original_output_status = run.output_status
    evidence, baselined_directories = _previous_output_evidence(
        original_manifest,
        run_root=run_root,
    )

    output_manifest, output_status, output_error = build_output_manifest(
        run,
        run.outputs if isinstance(run.outputs, dict) else {},
    )
    if output_error or output_status != AnalysisRun.OutputStatus.COMPLETE:
        raise ValueError("历史输出无法生成完整的固化清单。")
    _assert_preserved_output_evidence(evidence, output_manifest)
    now = timezone.now().isoformat()
    if original_manifest:
        if original_manifest.get("created_at"):
            output_manifest["created_at"] = original_manifest["created_at"]
        output_manifest["provenance"] = {
            "kind": "completion_manifest_upgrade",
            "source": source,
            "upgraded_at": now,
            "baselined_directory_keys": baselined_directories,
            "previous_manifest_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    original_manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        event_message = "已保留原完整性证据并升级历史输出清单。"
    else:
        output_manifest["provenance"] = {
            "kind": "historical_backfill",
            "source": source,
            "baselined_at": now,
        }
        event_message = "已为无历史清单的运行补建输出完整性基线。"

    with transaction.atomic():
        locked = AnalysisRun.objects.select_for_update().get(pk=run.pk)
        if not output_manifest_is_current(locked.output_manifest):
            if locked.status != AnalysisRun.Status.SUCCEEDED or not locked.work_directory:
                return False
            if (
                locked.work_directory != run.work_directory
                or locked.outputs != run.outputs
                or locked.output_manifest != original_manifest
                or locked.output_status != original_output_status
            ):
                return False
            locked.output_manifest = output_manifest
            locked.output_status = AnalysisRun.OutputStatus.COMPLETE
            locked.error = ""
            locked.error_code = ""
            locked.error_category = ""
            locked.error_retryable = False
            locked.error_details = {}
            locked.save(
                update_fields=[
                    "output_manifest",
                    "output_status",
                    "error",
                    "error_code",
                    "error_category",
                    "error_retryable",
                    "error_details",
                    "updated_at",
                ]
            )
            AnalysisRunEvent.objects.create(
                run=locked,
                kind="output_manifest_backfill",
                message=event_message,
                details={
                    "source": source,
                    "provenance_kind": output_manifest["provenance"]["kind"],
                },
            )
        run.output_manifest = locked.output_manifest
        run.output_status = locked.output_status
        run.error = locked.error
        run.error_code = locked.error_code
        run.error_category = locked.error_category
        run.error_retryable = locked.error_retryable
        run.error_details = locked.error_details
    return (
        output_manifest_is_current(run.output_manifest)
        and run.output_status == AnalysisRun.OutputStatus.COMPLETE
    )


def assert_output_snapshot_storage_writable() -> None:
    root = Path(settings.ANALYSIS_RUN_ROOT)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=root,
            prefix=".snapshot-write-check-",
        ):
            pass
    except OSError as error:
        raise ValueError(
            "运行根目录不可写；请在挂载了可写 runs 目录的 worker 容器中执行回填。"
        ) from error


def public_output_manifest(run: AnalysisRun) -> list[dict[str, Any]]:
    results = []
    manifest = run.output_manifest if isinstance(run.output_manifest, dict) else {}
    items = manifest.get("items")
    if not isinstance(items, list):
        return results
    has_integrity_v2 = output_manifest_has_integrity_v2(manifest)
    public_fields = (
        "key",
        "name",
        "label",
        "semantic_type",
        "wdl_type",
        "required",
        "kind",
        "reason",
        "filename",
        "size",
        "content_type",
        "sha256",
        "entry_count",
        "digest",
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        public = {
            key: _public_value(value)
            for key in public_fields
            if (value := item.get(key)) is not None
        }
        if item.get("kind") == "value" and "value" in item:
            reason = output_value_limit_reason(item["value"])
            if reason is None:
                public["value"] = _public_value(item["value"])
            else:
                public["kind"] = "unverifiable"
                public["reason"] = reason
        if (
            has_integrity_v2
            and output_manifest_file_item_is_verified(item)
        ):
            public["download_url"] = (
                f"/api/v1/integration/analysis-runs/{run.id}/outputs"
                f"/download?key={quote(str(item.get('key') or ''), safe='')}"
            )
        results.append(public)
    return results

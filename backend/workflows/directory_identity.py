from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class DirectoryIdentityLimitError(ValueError):
    pass


class DirectoryIdentityChangedError(ValueError):
    pass


def _identity(value: os.stat_result) -> dict[str, int]:
    return {
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "device": value.st_dev,
        "inode": value.st_ino,
    }


def scan_directory_identity(
    path: Path,
    *,
    max_entries: int,
    max_depth: int,
    deadline_seconds: float | None = None,
    checkpoint: Callable[[], None] | None = None,
    claim_entry: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Return a bounded deterministic POSIX identity digest for one directory."""

    root = path.absolute()
    digest = hashlib.sha256()
    entry_count = 0
    entry_limit = max(1, int(max_entries))
    depth_limit = max(1, int(max_depth))
    deadline = (
        time.monotonic() + max(0.0, deadline_seconds)
        if deadline_seconds is not None
        else None
    )

    def check_deadline() -> None:
        if checkpoint is not None:
            checkpoint()
        if deadline is not None and time.monotonic() >= deadline:
            raise DirectoryIdentityLimitError(
                f"目录快照超过时间上限：{path}"
            )

    def record(kind: str, relative: str, value: os.stat_result) -> None:
        check_deadline()
        item = {
            "kind": kind,
            "path": relative,
            "size": value.st_size if kind == "file" else None,
            "mtime_ns": value.st_mtime_ns,
            "ctime_ns": value.st_ctime_ns,
            "device": value.st_dev,
            "inode": value.st_ino,
        }
        digest.update(
            json.dumps(
                item,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )

    def scan_names(descriptor: int) -> list[str]:
        nonlocal entry_count
        names: list[str] = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                check_deadline()
                if claim_entry is not None:
                    claim_entry()
                entry_count += 1
                if entry_count > entry_limit:
                    raise DirectoryIdentityLimitError(
                        f"目录条目超过安全上限（{entry_limit}）：{path}"
                    )
                names.append(entry.name)
        names.sort()
        return names

    root_descriptor = os.open(root, directory_flags)
    root_before = os.fstat(root_descriptor)
    if not stat_module.S_ISDIR(root_before.st_mode):
        os.close(root_descriptor)
        raise ValueError(f"资源不是受支持的目录：{path}")
    frames: list[dict[str, Any]] = []
    try:
        frames.append(
            {
                "descriptor": root_descriptor,
                "relative": "",
                "identity": _identity(root_before),
                "names": scan_names(root_descriptor),
                "index": 0,
            }
        )
        root_descriptor = -1
        while frames:
            check_deadline()
            frame = frames[-1]
            names = frame["names"]
            index = frame["index"]
            descriptor = frame["descriptor"]
            if index >= len(names):
                after = os.fstat(descriptor)
                if _identity(after) != frame["identity"]:
                    raise DirectoryIdentityChangedError(
                        f"目录在快照期间发生变化：{frame['relative'] or path}"
                    )
                os.close(descriptor)
                frames.pop()
                continue
            name = names[index]
            frame["index"] = index + 1
            relative = (
                f"{frame['relative']}/{name}" if frame["relative"] else name
            )
            child_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat_module.S_ISLNK(child_stat.st_mode):
                raise ValueError(f"目录包含不受支持的符号链接：{relative}")
            if stat_module.S_ISREG(child_stat.st_mode):
                record("file", relative, child_stat)
                continue
            if not stat_module.S_ISDIR(child_stat.st_mode):
                raise ValueError(f"目录包含不受支持的节点：{relative}")
            if len(frames) >= depth_limit:
                raise DirectoryIdentityLimitError(
                    f"目录深度超过安全上限（{depth_limit}）：{relative}"
                )
            child_descriptor = os.open(name, directory_flags, dir_fd=descriptor)
            try:
                child_before = os.fstat(child_descriptor)
                if _identity(child_before) != _identity(child_stat):
                    raise DirectoryIdentityChangedError(
                        f"目录在快照期间发生变化：{relative}"
                    )
                record("directory", relative, child_before)
                child_names = scan_names(child_descriptor)
            except Exception:
                os.close(child_descriptor)
                raise
            frames.append(
                {
                    "descriptor": child_descriptor,
                    "relative": relative,
                    "identity": _identity(child_before),
                    "names": child_names,
                    "index": 0,
                }
            )

        check_deadline()
        current_root = os.stat(root, follow_symlinks=False)
        if _identity(current_root) != _identity(root_before):
            raise DirectoryIdentityChangedError(
                f"目录在快照期间发生变化：{path}"
            )
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        for frame in frames:
            try:
                os.close(frame["descriptor"])
            except OSError:
                pass
    return {
        "algorithm": "sha256-tree-identity-v1",
        "digest": "sha256:" + digest.hexdigest(),
        "entry_count": entry_count,
        "identity": {
            "mtime_ns": root_before.st_mtime_ns,
            "ctime_ns": root_before.st_ctime_ns,
            "device": root_before.st_dev,
            "inode": root_before.st_ino,
        },
    }

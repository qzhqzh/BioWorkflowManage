"""可选的人工复制完成协议，复用索引发现，仅对已发现批次做有界核验。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import transaction

from .models import RawdataBatchReadiness, RawdataDatasetIndex
from .rawdata_catalog import FASTQ_PATTERN, FASTQ_SUFFIX_PATTERN

MARKER = "_READY.done"


def _digest(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _identity(value):
    return [
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    ]


def _result(status, code=None, message=None, snapshot=None):
    return {
        "status": status,
        "submission_allowed": status == "ready",
        "reasons": [{"code": code, "message": message}] if code else [],
        "snapshot": snapshot,
    }


def _observe(directory):
    root = Path(settings.ANALYSIS_RAWDATA_ROOT).resolve(strict=True)
    relative = PurePosixPath(directory)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("批次目录必须位于 rawdata 根目录内。")
    batch = root / directory
    # 拒绝路径中的符号链接，防止挂载边界和批次身份发生隐式切换。
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("批次目录不能包含符号链接。")
    batch.resolve(strict=True).relative_to(root)
    marker = batch / MARKER
    try:
        marker_stat = marker.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(marker_stat.st_mode) or not os.access(marker, os.R_OK):
        raise ValueError("_READY.done 必须是可读普通文件，不能是目录或符号链接。")
    entries = []
    pairs = {}
    with os.scandir(batch) as iterator:
        for count, entry in enumerate(iterator, 1):
            if count > settings.RAWDATA_SCAN_MAX_ENTRIES:
                raise ValueError("批次目录超过核验预算，请拆分批次目录。")
            if not FASTQ_SUFFIX_PATTERN.search(entry.name):
                continue
            value = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode) or not os.access(entry.path, os.R_OK):
                raise ValueError("FASTQ 必须是可读普通文件，不能是符号链接。")
            if value.st_size == 0:
                raise ValueError("批次存在空 FASTQ 文件。")
            if value.st_mtime_ns > marker_stat.st_mtime_ns:
                raise ValueError(
                    "FASTQ 修改时间晚于 _READY.done，请完成复制后重新创建标记。"
                )
            if value.st_ctime_ns > marker_stat.st_ctime_ns:
                raise ValueError(
                    "FASTQ 状态变化晚于 _READY.done，请完成复制后重新创建标记。"
                )
            match = FASTQ_PATTERN.fullmatch(entry.name)
            if match is None:
                raise ValueError("批次存在无法识别 R1/R2 的 FASTQ 文件。")
            pair = (match["prefix"], match["suffix"])
            mates = pairs.setdefault(pair, [])
            mates.append(match["mate"])
            entries.append([entry.name, _identity(value)])
    if not pairs or any(sorted(mates) != ["1", "2"] for mates in pairs.values()):
        raise ValueError("批次存在缺失或重复的 R1/R2 配对文件。")
    if _identity(marker.lstat()) != _identity(marker_stat):
        raise ValueError("核验期间完成标记发生变化，请重试。")
    return _digest(_identity(marker_stat)), _digest(sorted(entries))


def _invalidate_current(root_key, directory):
    try:
        marker = Path(settings.ANALYSIS_RAWDATA_ROOT) / directory / MARKER
        value = marker.lstat()
        if stat.S_ISREG(value.st_mode):
            return RawdataBatchReadiness.objects.filter(
                root_key=root_key,
                directory=directory,
                generation=_digest(_identity(value)),
            ).update(changed=True)
    except OSError:
        pass
    return 0


def observe_indexed_batches(root_key, datasets):
    """只在现有索引完整扫描结束时建立新标记代次的基线。"""
    directories = {str(Path(item["pair_key"]).parent) for item in datasets}
    for directory in sorted(directories):
        try:
            generation, inventory = _observe(directory)
        except (OSError, ValueError):
            # 无效/缺失状态不能擦除旧基线或解除 changed 锁存。
            _invalidate_current(root_key, directory)
            continue
        if generation is None:
            continue
        # 扫描与实时核验之间发生修改时不以过期索引建立基线。
        try:
            root = Path(settings.ANALYSIS_RAWDATA_ROOT)
            for dataset in datasets:
                if str(Path(dataset["pair_key"]).parent) != directory:
                    continue
                for item in dataset["files"]:
                    value = (root / item["relative_path"]).stat()
                    identity = item.get("identity", {})
                    if any(
                        identity.get(key) != actual
                        for key, actual in (
                            ("size", value.st_size),
                            ("mtime_ns", value.st_mtime_ns),
                            ("ctime_ns", value.st_ctime_ns),
                            ("device", value.st_dev),
                            ("inode", value.st_ino),
                        )
                    ):
                        raise ValueError("扫描期间文件变化")
            if _observe(directory) != (generation, inventory):
                raise ValueError("扫描期间批次变化")
        except (OSError, ValueError):
            _invalidate_current(root_key, directory)
            continue
        with transaction.atomic():
            state, created = (
                RawdataBatchReadiness.objects.select_for_update().get_or_create(
                    root_key=root_key,
                    directory=directory,
                    defaults={"generation": generation, "inventory_digest": inventory},
                )
            )
            if not created:
                if state.generation != generation:
                    state.generation = generation
                    state.inventory_digest = inventory
                    state.changed = False
                elif state.inventory_digest != inventory:
                    state.changed = True
                state.save()


def _batch_snapshot(root_key, directory):
    try:
        generation, inventory = _observe(directory)
    except (OSError, ValueError) as error:
        if _invalidate_current(root_key, directory):
            return _result(
                "changed",
                "RAWDATA_BATCH_CHANGED",
                "完成标记建立后数据发生变化，请核对并重新创建标记或使用新批次。",
            )
        return _result("issue", "RAWDATA_BATCH_INVALID", str(error))
    if generation is None:
        return _result(
            "waiting",
            "RAWDATA_MARKER_MISSING",
            "等待复制完成：请在批次目录创建 _READY.done。",
        )
    state = RawdataBatchReadiness.objects.filter(
        root_key=root_key, directory=directory
    ).first()
    if state is None or state.generation != generation:
        return _result(
            "unknown",
            "RAWDATA_MARKER_NOT_INDEXED",
            "完成标记已存在，等待后台索引确认。",
        )
    if state.changed or state.inventory_digest != inventory:
        RawdataBatchReadiness.objects.filter(pk=state.pk, generation=generation).update(
            changed=True
        )
        return _result(
            "changed",
            "RAWDATA_BATCH_CHANGED",
            "完成标记建立后数据发生变化，请核对并重新创建标记或使用新批次。",
        )
    return _result(
        "ready",
        snapshot={
            "directory": directory,
            "generation": generation,
            "digest": inventory,
        },
    )


def rawdata_readiness(files, *, require_fresh_index=True, context=None, index_root_key=None):
    from .rawdata_index import indexed_rawdata_catalog, rawdata_root_key

    if (
        not isinstance(files, list)
        or len(files) > 64
        or any(
            not isinstance(item, str)
            or not item
            or PurePosixPath(item).is_absolute()
            or ".." in PurePosixPath(item).parts
            or str(PurePosixPath(item)) != item
            for item in files
        )
        or len(files) != len(set(files))
    ):
        return _result(
            "issue",
            "RAWDATA_FILES_INVALID",
            "files 必须为至多 64 个不重复的规范相对路径。",
        )
    if not files:
        return _result("unbound", "RAWDATA_NOT_BOUND", "尚未关联 FASTQ 数据。")
    root_key = index_root_key or rawdata_root_key()
    if require_fresh_index:
        catalog = context["catalog"] if context else indexed_rawdata_catalog()
        index = catalog["index"]
        if (
            not index["snapshot_scan_id"]
            or index["stale"]
            or index["latest_status"] != "succeeded"
            or catalog.get("scan_limited")
        ):
            return _result(
                "unknown",
                "RAWDATA_INDEX_UNAVAILABLE",
                "数据索引尚未就绪、过期或扫描失败，请稍后重试。",
            )
    wanted = set(files)
    matched = []
    found = set()
    datasets = (
        {
            dataset.pk: dataset
            for path in files
            for dataset in context["by_path"].get(path, [])
        }.values()
        if context
        else RawdataDatasetIndex.objects.filter(root_key=root_key, active=True)
    )
    for dataset in datasets:
        paths = {item.get("relative_path") for item in dataset.files}
        if paths & wanted:
            matched.append(dataset)
            found |= paths & wanted
            if not paths <= wanted:
                return _result(
                    "issue",
                    "RAWDATA_PAIR_INCOMPLETE",
                    "必须选择完整的 R1/R2 配对文件。",
                )
    if found != wanted:
        return _result(
            "unknown",
            "RAWDATA_FILES_NOT_INDEXED",
            "部分文件不在当前索引中，请确认路径并等待索引刷新。",
        )
    batches = []
    for directory in sorted({str(Path(item.pair_key).parent) for item in matched}):
        if context is not None:
            cache = context.setdefault("batches", {})
            if directory not in cache:
                cache[directory] = _batch_snapshot(root_key, directory)
            result = cache[directory]
        else:
            result = _batch_snapshot(root_key, directory)
        if not result["submission_allowed"]:
            return result
        batches.append(result["snapshot"])
    for dataset in matched:
        if dataset.status != "ready" or dataset.issues:
            return {
                **_result("issue"),
                "reasons": dataset.issues
                or [
                    {
                        "code": "RAWDATA_DATASET_INVALID",
                        "message": "数据配对未通过索引检查。",
                    }
                ],
            }
    return _result(
        "ready",
        snapshot={"schema_version": 1, "root_key": root_key, "files": sorted(files), "batches": batches},
    )


def readiness_context():
    from .rawdata_index import indexed_rawdata_catalog, rawdata_root_key

    datasets = list(
        RawdataDatasetIndex.objects.filter(root_key=rawdata_root_key(), active=True)
    )
    by_path = {}
    for dataset in datasets:
        for item in dataset.files:
            by_path.setdefault(item.get("relative_path"), []).append(dataset)
    return {
        "catalog": indexed_rawdata_catalog(),
        "by_path": by_path,
    }


def verify_readiness_snapshot(snapshot, manifest, *, require_fresh_index=True):
    from .rawdata_index import rawdata_root_key

    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("RAWDATA_SNAPSHOT_INVALID: 必须提供有效的数据就绪快照。")
    root_key = snapshot.get("root_key", rawdata_root_key())
    if (
        not isinstance(root_key, str)
        or len(root_key) != 71
        or not root_key.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in root_key[7:])
        or (require_fresh_index and root_key != rawdata_root_key())
    ):
        raise ValueError("RAWDATA_SNAPSHOT_INVALID: 数据索引根与当前服务不一致。")
    paths = sorted(
        item.get("relative_path", "") for item in (manifest or {}).get("files", [])
    )
    if snapshot.get("files") != paths or (manifest or {}).get("objects"):
        raise ValueError(
            "RAWDATA_SNAPSHOT_INPUT_MISMATCH: 就绪快照与实际 rawdata 输入不一致。"
        )
    # API 与 worker 可使用不同挂载路径；索引键固定为接受端的键，
    # 实际文件仍仅在当前 worker 的受管根内核验，不能用快照提供绝对路径。
    result = rawdata_readiness(
        paths, require_fresh_index=require_fresh_index, index_root_key=root_key
    )
    if not result["submission_allowed"]:
        raise ValueError(
            "RAWDATA_NOT_READY: "
            + "; ".join(item["message"] for item in result["reasons"])
        )
    if "root_key" not in snapshot:
        result["snapshot"].pop("root_key", None)
    if result["snapshot"] != snapshot:
        raise ValueError(
            "RAWDATA_SNAPSHOT_CHANGED: 数据或完成标记已变化，请重新检查后提交。"
        )
    return result["snapshot"]

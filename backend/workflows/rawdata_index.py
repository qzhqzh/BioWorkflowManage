from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    AnalysisRun,
    RawdataDatasetEvent,
    RawdataDatasetIndex,
    RawdataRunReference,
    RawdataScan,
)
from .rawdata_catalog import (
    _empty_catalog,
    _issue,
    advance_rawdata_scan,
    public_rawdata_catalog,
)


def rawdata_root_key(root_value: str | Path | None = None) -> str:
    root = Path(root_value or settings.ANALYSIS_RAWDATA_ROOT).resolve(strict=False)
    return "sha256:" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()


def _identity_digest(dataset: dict[str, Any]) -> str:
    payload = [
        {
            "relative_path": item.get("relative_path"),
            "identity": item.get("identity", {}),
        }
        for item in dataset.get("files", [])
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dataset_event_payload(dataset: RawdataDatasetIndex) -> dict[str, Any]:
    return {
        "status": dataset.status,
        "pair_key": dataset.pair_key,
        "identity_digest": dataset.identity_digest,
        "active": dataset.active,
        "files": [
            {
                "relative_path": item.get("relative_path"),
                "identity": item.get("identity", {}),
            }
            for item in dataset.files
        ],
    }


def _apply_catalog(scan: RawdataScan, catalog: dict[str, Any]) -> None:
    now = scan.finished_at or timezone.now()
    seen_ids: set[int] = set()
    for payload in catalog.get("datasets", []):
        dataset = (
            RawdataDatasetIndex.objects.select_for_update()
            .filter(root_key=scan.root_key, dataset_id=payload["id"])
            .first()
        )
        identity_digest = _identity_digest(payload)
        if dataset is None:
            dataset = RawdataDatasetIndex.objects.create(
                root_key=scan.root_key,
                dataset_id=payload["id"],
                pair_key=payload["pair_key"],
                name=payload["name"],
                directory=payload["directory"],
                status=payload["status"],
                issues=payload["issues"],
                files=payload["files"],
                total_size=payload["total_size"],
                identity_digest=identity_digest,
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                last_changed_at=now,
                last_scan=scan,
            )
            RawdataDatasetEvent.objects.create(
                dataset=dataset,
                scan=scan,
                action="discovered",
                actor=scan.actor,
                after=_dataset_event_payload(dataset),
            )
        else:
            before = _dataset_event_payload(dataset)
            action = None
            if not dataset.active:
                action = "restored"
            elif (
                dataset.identity_digest != identity_digest
                or dataset.status != payload["status"]
                or dataset.issues != payload["issues"]
                or dataset.pair_key != payload["pair_key"]
            ):
                action = "changed"
            dataset.pair_key = payload["pair_key"]
            dataset.name = payload["name"]
            dataset.directory = payload["directory"]
            dataset.status = payload["status"]
            dataset.issues = payload["issues"]
            dataset.files = payload["files"]
            dataset.total_size = payload["total_size"]
            dataset.identity_digest = identity_digest
            dataset.active = True
            dataset.last_seen_at = now
            dataset.last_scan = scan
            if action:
                dataset.last_changed_at = now
            dataset.save()
            if action:
                RawdataDatasetEvent.objects.create(
                    dataset=dataset,
                    scan=scan,
                    action=action,
                    actor=scan.actor,
                    before=before,
                    after=_dataset_event_payload(dataset),
                )
        seen_ids.add(dataset.id)

    if catalog.get("root_status") != "ready" or catalog.get("scan_limited"):
        return
    missing = list(
        RawdataDatasetIndex.objects.select_for_update()
        .filter(root_key=scan.root_key, active=True)
        .exclude(id__in=seen_ids)
    )
    for dataset in missing:
        before = _dataset_event_payload(dataset)
        dataset.active = False
        dataset.last_changed_at = now
        dataset.last_scan = scan
        dataset.save(update_fields=["active", "last_changed_at", "last_scan"])
        RawdataDatasetEvent.objects.create(
            dataset=dataset,
            scan=scan,
            action="missing",
            actor=scan.actor,
            before=before,
            after=_dataset_event_payload(dataset),
        )


def queue_rawdata_scan(
    *,
    actor: str,
    trigger: str,
    root_value: str | Path | None = None,
    minimum_interval_seconds: int = 0,
) -> tuple[RawdataScan, bool]:
    root_key = rawdata_root_key(root_value)
    with transaction.atomic():
        active = (
            RawdataScan.objects.select_for_update()
            .filter(
                root_key=root_key,
                status__in=[RawdataScan.Status.QUEUED, RawdataScan.Status.RUNNING],
            )
            .first()
        )
        if active is not None:
            return active, False
        if minimum_interval_seconds > 0:
            recent = RawdataScan.objects.filter(
                root_key=root_key,
                status__in=[
                    RawdataScan.Status.SUCCEEDED,
                    RawdataScan.Status.LIMITED,
                ],
                finished_at__gte=timezone.now()
                - timedelta(seconds=minimum_interval_seconds),
            ).first()
            if recent is not None:
                return recent, False
        try:
            # Keep the unique-constraint race inside a savepoint so the outer
            # transaction remains usable when another worker wins the enqueue.
            with transaction.atomic():
                scan = RawdataScan.objects.create(
                    root_key=root_key,
                    actor=actor[:256],
                    trigger=trigger[:24],
                )
            return scan, True
        except IntegrityError:
            return (
                RawdataScan.objects.get(
                    root_key=root_key,
                    status__in=[RawdataScan.Status.QUEUED, RawdataScan.Status.RUNNING],
                ),
                False,
            )


def ensure_periodic_rawdata_scan(
    root_value: str | Path | None = None,
) -> tuple[RawdataScan | None, bool]:
    root_key = rawdata_root_key(root_value)
    active = RawdataScan.objects.filter(
        root_key=root_key,
        status__in=[RawdataScan.Status.QUEUED, RawdataScan.Status.RUNNING],
    ).first()
    if active is not None:
        return active, False
    latest = RawdataScan.objects.filter(
        root_key=root_key,
        status__in=[
            RawdataScan.Status.SUCCEEDED,
            RawdataScan.Status.LIMITED,
            RawdataScan.Status.FAILED,
        ],
    ).first()
    interval = timedelta(seconds=settings.RAWDATA_INDEX_INTERVAL_SECONDS)
    if latest and latest.finished_at and timezone.now() - latest.finished_at < interval:
        return None, False
    return queue_rawdata_scan(actor="system", trigger="scheduled", root_value=root_value)


def _claim_scan(root_value: str | Path | None = None):
    root_key = rawdata_root_key(root_value)
    now = timezone.now()
    with transaction.atomic():
        scan = (
            RawdataScan.objects.select_for_update()
            .filter(root_key=root_key)
            .filter(
                Q(status=RawdataScan.Status.QUEUED)
                | Q(
                    status=RawdataScan.Status.RUNNING,
                    lease_expires_at__lt=now,
                )
                | Q(
                    status=RawdataScan.Status.RUNNING,
                    lease_expires_at__isnull=True,
                )
            )
            .order_by("created_at")
            .first()
        )
        if scan is None:
            return None
        token = uuid.uuid4()
        scan.status = RawdataScan.Status.RUNNING
        scan.started_at = scan.started_at or now
        scan.attempt_count += 1
        scan.lease_token = token
        scan.heartbeat_at = now
        scan.lease_expires_at = now + timedelta(
            seconds=settings.RAWDATA_SCAN_LEASE_SECONDS
        )
        scan.save()
        return scan, token


def run_rawdata_scan_batch(root_value: str | Path | None = None) -> RawdataScan | None:
    claimed = _claim_scan(root_value)
    if claimed is None:
        return None
    scan, token = claimed
    root = Path(root_value or settings.ANALYSIS_RAWDATA_ROOT)
    try:
        progress, catalog = advance_rawdata_scan(
            root,
            scan.progress,
            batch_entries=settings.RAWDATA_SCAN_BATCH_ENTRIES,
            max_files=settings.RAWDATA_SCAN_MAX_FILES,
            max_entries=settings.RAWDATA_SCAN_MAX_ENTRIES,
            max_depth=settings.RAWDATA_SCAN_MAX_DEPTH,
            deadline_seconds=settings.RAWDATA_SCAN_BATCH_SECONDS,
        )
    except Exception as error:  # pragma: no cover - defensive worker boundary
        with transaction.atomic():
            locked = RawdataScan.objects.select_for_update().filter(
                pk=scan.pk,
                lease_token=token,
            ).first()
            if locked is None:
                return scan
            locked.status = RawdataScan.Status.FAILED
            locked.error = f"{type(error).__name__}: rawdata scan failed"[:1000]
            locked.finished_at = timezone.now()
            locked.lease_token = None
            locked.lease_expires_at = None
            locked.save()
            return locked

    with transaction.atomic():
        locked = RawdataScan.objects.select_for_update().filter(
            pk=scan.pk,
            lease_token=token,
        ).first()
        if locked is None:
            return scan
        now = timezone.now()
        locked.heartbeat_at = now
        locked.lease_token = None
        locked.lease_expires_at = None
        if catalog is None:
            locked.progress = progress
            locked.scanned_entry_count = int(progress.get("scanned_entries", 0))
            locked.save()
            return locked
        locked.progress = {}
        locked.catalog = catalog
        locked.scanned_entry_count = int(catalog.get("scanned_entry_count", 0))
        locked.scan_limited = bool(catalog.get("scan_limited"))
        locked.status = (
            RawdataScan.Status.LIMITED
            if locked.scan_limited
            else RawdataScan.Status.SUCCEEDED
        )
        locked.finished_at = now
        locked.save()
        _apply_catalog(locked, catalog)
        return locked


def _empty_index_catalog() -> dict[str, Any]:
    catalog = _empty_catalog(
        "indexing",
        _issue(
            "RAWDATA_INDEX_PENDING",
            "原始数据索引尚未建立，后台扫描完成后会自动显示。",
        ),
    )
    catalog["scanned_at"] = None
    return catalog


def _augment_dataset_history(catalog: dict[str, Any], root_key: str) -> None:
    indexed = {
        item.dataset_id: item
        for item in RawdataDatasetIndex.objects.filter(
            root_key=root_key,
            active=True,
        ).prefetch_related("run_references__run")
    }
    for payload in catalog.get("datasets", []):
        dataset = indexed.get(payload["id"])
        if dataset is None:
            continue
        references = list(dataset.run_references.all())
        payload["first_seen_at"] = dataset.first_seen_at.isoformat()
        payload["last_seen_at"] = dataset.last_seen_at.isoformat()
        payload["last_changed_at"] = dataset.last_changed_at.isoformat()
        payload["run_count"] = len(references)
        payload["recent_runs"] = [
            {
                "id": str(item.run_id),
                "status": item.run.status,
                "created_at": item.run.created_at.isoformat(),
            }
            for item in references[:3]
        ]


def indexed_rawdata_catalog(
    root_value: str | Path | None = None,
) -> dict[str, Any]:
    root_key = rawdata_root_key(root_value)
    latest = RawdataScan.objects.filter(
        root_key=root_key,
        status__in=[
            RawdataScan.Status.SUCCEEDED,
            RawdataScan.Status.LIMITED,
            RawdataScan.Status.FAILED,
        ],
    ).first()
    snapshot = RawdataScan.objects.filter(
        root_key=root_key,
        status=RawdataScan.Status.SUCCEEDED,
    ).first()
    active = RawdataScan.objects.filter(
        root_key=root_key,
        status__in=[RawdataScan.Status.QUEUED, RawdataScan.Status.RUNNING],
    ).first()
    catalog_source = snapshot or (
        latest if latest and latest.status == RawdataScan.Status.LIMITED else None
    )
    catalog = (
        deepcopy(catalog_source.catalog)
        if catalog_source and catalog_source.catalog
        else _empty_index_catalog()
    )
    if latest and latest.status == RawdataScan.Status.LIMITED and snapshot:
        catalog["scan_limited"] = True
        catalog["issues"] = latest.catalog.get("issues", [])
        catalog["scanned_entry_count"] = latest.scanned_entry_count
    _augment_dataset_history(catalog, root_key)
    public = public_rawdata_catalog(catalog)
    now = timezone.now()
    finished_at = latest.finished_at if latest else None
    snapshot_finished_at = snapshot.finished_at if snapshot else None
    stale = bool(
        snapshot_finished_at
        and now - snapshot_finished_at
        >= timedelta(seconds=settings.RAWDATA_INDEX_STALE_SECONDS)
    )
    repair_suggestions = []
    if catalog.get("scan_limited"):
        repair_suggestions.append(
            "扫描未完整完成；请检查目录权限和层级，必要时拆分超宽目录或调整扫描阈值。"
        )
    if catalog.get("root_status") in {"missing", "unreadable"}:
        repair_suggestions.append("检查 rawdata 只读挂载与容器用户的目录读取权限。")
    if latest and latest.status == RawdataScan.Status.FAILED:
        repair_suggestions.append(
            "最近一次后台扫描失败；请查看 rawdata-indexer 日志并检查目录读取权限。"
        )
    public["index"] = {
        "latest_scan_id": str(latest.id) if latest else None,
        "latest_status": latest.status if latest else None,
        "snapshot_scan_id": str(snapshot.id) if snapshot else None,
        "active_scan_id": str(active.id) if active else None,
        "active_status": active.status if active else None,
        "queued_at": active.created_at.isoformat() if active else None,
        "started_at": active.started_at.isoformat() if active and active.started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "stale": stale,
        "policy": {
            "max_files": settings.RAWDATA_SCAN_MAX_FILES,
            "max_entries": settings.RAWDATA_SCAN_MAX_ENTRIES,
            "max_depth": settings.RAWDATA_SCAN_MAX_DEPTH,
            "batch_entries": settings.RAWDATA_SCAN_BATCH_ENTRIES,
        },
        "repair_suggestions": repair_suggestions,
    }
    return public


def link_run_to_indexed_dataset(
    *,
    run: AnalysisRun,
    dataset_id: str,
    identity: dict[str, Any],
) -> RawdataRunReference | None:
    dataset = RawdataDatasetIndex.objects.filter(
        root_key=rawdata_root_key(),
        dataset_id=dataset_id,
        active=True,
    ).first()
    if dataset is None:
        return None
    with transaction.atomic():
        reference, created = RawdataRunReference.objects.get_or_create(
            dataset=dataset,
            run=run,
            defaults={"identity": identity},
        )
        if created:
            RawdataDatasetEvent.objects.create(
                dataset=dataset,
                run=run,
                action="run_linked",
                actor=run.actor,
                after={"run_id": str(run.id), "identity": identity},
            )
    return reference

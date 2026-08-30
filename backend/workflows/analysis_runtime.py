from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat as stat_module
import subprocess
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from .integration_outputs import (
    _directory_manifest,
    _file_identity,
    _open_regular_readonly,
    _sha256,
    build_output_manifest,
    ResourceSnapshotBudget,
    ResourceSnapshotBudgetError,
)
from .models import AnalysisRun, AnalysisRunEvent
from .object_inputs import (
    ObjectInputError,
    object_manifest_items,
    stage_run_object_inputs,
    verify_run_object_inputs,
)
from .webhooks import enqueue_terminal_event
from .wdl_packages import normalize_package_path
from .wdl_source_references import (
    effective_package_files,
    reference_specs_for_revision,
)


GIB = 1024**3
INFRASTRUCTURE_ERROR_PATTERNS = (
    "cannot connect to the docker daemon",
    "connection reset by peer",
    "context deadline exceeded",
    "deadlineexceeded",
    "docker task running, exit code = -1",
    "error during connect",
    "heartbeat to manager failed",
    "interrupted",
    "agent session failed",
    "node is not a swarm manager",
    "rpc error",
)


class AnalysisRunLeaseLost(RuntimeError):
    pass


class WorkflowPackageTrustError(RuntimeError):
    code = "ANALYSIS_WORKFLOW_PACKAGE_UNTRUSTED"
    category = "security"
    retryable = False


class AnalysisProductTrustError(RuntimeError):
    code = "ANALYSIS_PRODUCT_REQUIRED"
    category = "security"
    retryable = False


def _require_trusted_analysis_source(run: AnalysisRun) -> None:
    if run.run_kind != AnalysisRun.Kind.WORKFLOW:
        return
    if settings.INTEGRATION_REQUIRE_ANALYSIS_PRODUCT:
        from .analysis_products import analysis_product_version_is_current

        product_version = run.analysis_product_version
        if (
            product_version is None
            or product_version.workflow_version_id != run.workflow_version_id
            or product_version.source_digest != run.source_digest
            or not analysis_product_version_is_current(product_version)
        ):
            raise AnalysisProductTrustError(
                "当前部署只允许执行仍有效且与运行快照一致的 Analysis Product。"
            )
    if not settings.INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE:
        return
    if not run.workflow_version_id:
        raise WorkflowPackageTrustError(
            "历史 WDL 资产没有工作流包签名证明，当前部署禁止执行。"
        )
    from .analysis_products import workflow_package_attestation_is_current

    if (
        run.source_digest != run.workflow_version.compiled_digest
        or not workflow_package_attestation_is_current(run.workflow_version)
    ):
        raise WorkflowPackageTrustError(
            "运行快照与 WorkflowVersion 固定编译产物或工作流包签名证明不一致。"
        )


def _terminate_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        return


def _cleanup_swarm_services_for_run(
    run_directory: Path,
    *,
    docker_client=None,
) -> tuple[list[str], list[str]]:
    """Remove only miniwdl Swarm services mounting this AnalysisRun directory."""

    root = run_directory.resolve()
    created_client = docker_client is None
    removed: list[str] = []
    errors: list[str] = []
    try:
        if docker_client is None:
            import docker

            docker_client = docker.from_env(timeout=10)
        services = docker_client.services.list(filters={"label": "miniwdl_run_id"})
        for service in services:
            specification = service.attrs.get("Spec") or {}
            labels = specification.get("Labels") or {}
            if "miniwdl_run_id" not in labels:
                continue
            container = (specification.get("TaskTemplate") or {}).get(
                "ContainerSpec"
            ) or {}
            belongs_to_run = False
            for mount in container.get("Mounts") or []:
                source = str(mount.get("Source") or "")
                if not source or not Path(source).is_absolute():
                    continue
                try:
                    Path(source).resolve().relative_to(root)
                    belongs_to_run = True
                    break
                except (OSError, ValueError):
                    continue
            if not belongs_to_run:
                continue
            name = str(getattr(service, "name", None) or service.id)
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    service.remove()
                    removed.append(name)
                    last_error = None
                    break
                except Exception as error:
                    last_error = error
                    if attempt < 2:
                        time.sleep(0.25)
            if last_error is not None:
                errors.append(f"{name}: {last_error}")
    except Exception as error:
        errors.append(str(error))
    finally:
        if created_client and docker_client is not None:
            try:
                docker_client.close()
            except Exception:
                pass
    return removed, errors


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
    lease_token = run.lease_token
    status_changed = "status" in values and values["status"] != run.status
    if "progress" in values:
        values["progress"] = max(run.progress, int(values["progress"]))
    if status_changed:
        values["status_version"] = F("status_version") + 1
    values["updated_at"] = timezone.now()
    with transaction.atomic():
        queryset = AnalysisRun.objects.filter(pk=run.pk)
        if lease_token is not None:
            queryset = queryset.filter(
                lease_token=lease_token,
                status__in=[AnalysisRun.Status.PREPARING, AnalysisRun.Status.RUNNING],
            )
        if queryset.update(**values) != 1:
            raise AnalysisRunLeaseLost(
                f"analysis run {run.id} lease is no longer active"
            )
        if status_changed:
            persisted_run = AnalysisRun.objects.get(pk=run.pk)
            enqueue_terminal_event(persisted_run)
    for name, value in values.items():
        if name != "status_version":
            setattr(run, name, value)
    if status_changed:
        run.status_version += 1


def _renew_lease(run: AnalysisRun) -> bool:
    if run.lease_token is None:
        return True
    now = timezone.now()
    updated = AnalysisRun.objects.filter(
        pk=run.pk,
        lease_token=run.lease_token,
        status__in=[AnalysisRun.Status.PREPARING, AnalysisRun.Status.RUNNING],
    ).update(
        worker_heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=settings.ANALYSIS_RUN_LEASE_SECONDS),
        updated_at=now,
    )
    return updated == 1


class _LeaseHeartbeat:
    def __init__(self, run: AnalysisRun):
        self.run = run
        self.stop = threading.Event()
        self.lease_lost = threading.Event()
        self.thread: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.process_lock = threading.Lock()
        self.lease_deadline = time.monotonic() + float(
            settings.ANALYSIS_RUN_LEASE_SECONDS
        )
        self.cleanup_errors: list[str] = []

    def __enter__(self):
        if self.run.lease_token is None:
            return self
        if not _renew_lease(self.run):
            raise AnalysisRunLeaseLost(
                f"analysis run {self.run.id} lease is no longer active"
            )
        self.lease_deadline = time.monotonic() + float(
            settings.ANALYSIS_RUN_LEASE_SECONDS
        )
        self.thread = threading.Thread(
            target=self._heartbeat,
            name=f"analysis-heartbeat-{self.run.id}",
            daemon=True,
        )
        self.thread.start()
        return self

    def _heartbeat(self) -> None:
        interval = settings.ANALYSIS_RUN_HEARTBEAT_SECONDS
        while not self.stop.wait(interval):
            close_old_connections()
            try:
                if not self._renew_or_expire():
                    return
            finally:
                close_old_connections()

    def _renew_or_expire(self) -> bool:
        try:
            renewed = _renew_lease(self.run)
        except Exception:
            if time.monotonic() < self.lease_deadline:
                return True
            renewed = False
        if renewed:
            self.lease_deadline = time.monotonic() + float(
                settings.ANALYSIS_RUN_LEASE_SECONDS
            )
            return True
        self.lease_lost.set()
        self._terminate_process()
        try:
            _finalize_cancelled_run(self.run.pk, self.run.lease_token)
        except Exception:
            # The database may be the reason renewal expired. The fenced process
            # is already stopped; stale-run recovery will converge the row later.
            pass
        return False

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=settings.ANALYSIS_RUN_HEARTBEAT_SECONDS + 1)

    def watch(self, process: subprocess.Popen) -> None:
        with self.process_lock:
            self.process = process
            if self.lease_lost.is_set():
                self._terminate_process_locked()

    def unwatch(self, process: subprocess.Popen) -> None:
        with self.process_lock:
            if self.process is process:
                self.process = None

    def _terminate_process(self) -> None:
        with self.process_lock:
            self._terminate_process_locked()

    def _terminate_process_locked(self) -> None:
        process = self.process
        if process is not None:
            _terminate_process_group(process)
        self.cleanup_services()

    def cleanup_services(self) -> None:
        work_directory = str(getattr(self.run, "work_directory", "") or "")
        if not work_directory:
            return
        _, errors = _cleanup_swarm_services_for_run(Path(work_directory))
        self.cleanup_errors.extend(errors)


def _finalize_cancelled_run(run_id, lease_token) -> bool:
    with transaction.atomic():
        run = AnalysisRun.objects.select_for_update().filter(pk=run_id).first()
        if run is None or run.status != AnalysisRun.Status.CANCEL_REQUESTED:
            return False
        if lease_token is not None and run.lease_token != lease_token:
            return False
        run.status = AnalysisRun.Status.CANCELED
        run.status_version += 1
        run.current_step = "运行已取消"
        run.error = "运行已按请求取消。"
        run.finished_at = timezone.now()
        run.output_status = AnalysisRun.OutputStatus.UNAVAILABLE
        run.error_code = "ANALYSIS_CANCELED"
        run.error_category = "cancellation"
        run.error_retryable = False
        run.error_details = {}
        run.lease_token = None
        run.worker_heartbeat_at = None
        run.lease_expires_at = None
        run.save(
            update_fields=[
                "status",
                "status_version",
                "current_step",
                "error",
                "finished_at",
                "output_status",
                "error_code",
                "error_category",
                "error_retryable",
                "error_details",
                "lease_token",
                "worker_heartbeat_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
        enqueue_terminal_event(run)
        _event(run, "运行已按请求取消。", kind="cancellation", level="warning")
        return True


def _recover_stale_runs(now) -> None:
    stale_runs = list(
        AnalysisRun.objects.select_for_update(skip_locked=True)
        .filter(
            status__in=[
                AnalysisRun.Status.PREPARING,
                AnalysisRun.Status.RUNNING,
                AnalysisRun.Status.CANCEL_REQUESTED,
            ],
            lease_expires_at__lt=now,
        )
        .order_by("lease_expires_at")[:20]
    )
    for run in stale_runs:
        stale_work_directory = str(run.work_directory or "")
        if run.status == AnalysisRun.Status.CANCEL_REQUESTED:
            run.status = AnalysisRun.Status.CANCELED
            run.current_step = "运行已取消"
            run.error = "运行已按请求取消。"
            run.finished_at = now
            run.output_status = AnalysisRun.OutputStatus.UNAVAILABLE
            run.error_code = "ANALYSIS_CANCELED"
            run.error_category = "cancellation"
            run.error_retryable = False
            run.error_details = {}
            level = "warning"
            message = "取消中的运行租约已过期，状态已收敛为已取消。"
        elif run.status == AnalysisRun.Status.PREPARING and not run.work_directory:
            run.status = AnalysisRun.Status.QUEUED
            run.current_step = "worker 中断，已安全重新排队"
            run.started_at = None
            run.error = ""
            run.error_code = ""
            run.error_category = ""
            run.error_retryable = False
            run.error_details = {}
            level = "warning"
            message = "准备阶段 worker 租约过期，运行已重新排队。"
        else:
            run.status = AnalysisRun.Status.FAILED
            run.progress = 100
            run.current_step = "worker 连接中断，需手动重跑"
            run.error = "worker 心跳超时；为避免重复执行，系统没有自动重跑。"
            run.error_code = "ANALYSIS_WORKER_LEASE_LOST"
            run.error_category = "infrastructure"
            run.error_retryable = True
            run.error_details = {"lease_expired_at": run.lease_expires_at.isoformat()}
            run.output_status = AnalysisRun.OutputStatus.UNAVAILABLE
            run.finished_at = now
            level = "error"
            message = "运行阶段 worker 租约过期，已停止自动恢复以避免重复任务。"
        run.status_version += 1
        run.lease_token = None
        run.worker_heartbeat_at = None
        run.lease_expires_at = None
        run.save()
        enqueue_terminal_event(run)
        _event(run, message, kind="lease", level=level)
        if stale_work_directory:
            transaction.on_commit(
                lambda work_directory=stale_work_directory: (
                    _cleanup_swarm_services_for_run(Path(work_directory))
                )
            )


def _available_memory_bytes(path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _resource_wait_message(run: AnalysisRun | None = None) -> str | None:
    minimum_gb = max(0.0, float(settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB))
    if run is not None and run.run_kind == AnalysisRun.Kind.TOOL_TEST:
        requested_gb = float(
            (run.tool_version.tool_spec.get("runtime") or {}).get("memory_gb") or 1
        )
        minimum_gb = min(minimum_gb, max(2.0, requested_gb * 1.25))
    if minimum_gb == 0:
        return None
    available = _available_memory_bytes()
    if available is None or available >= minimum_gb * GIB:
        return None
    return (
        f"等待计算资源（可用内存 {available / GIB:.1f} GB，"
        f"至少需要 {minimum_gb:g} GB）"
    )


def claim_next_run() -> AnalysisRun | None:
    with transaction.atomic():
        now = timezone.now()
        _recover_stale_runs(now)
        run = (
            AnalysisRun.objects.select_for_update(skip_locked=True)
            .filter(status=AnalysisRun.Status.QUEUED)
            .order_by("created_at")
            .first()
        )
        if run is None:
            return None
        if run.tool_version_id:
            run.tool_version
        wait_message = _resource_wait_message(run)
        if wait_message:
            if not run.current_step.startswith("等待计算资源"):
                run.current_step = wait_message
                run.save(update_fields=["current_step", "updated_at"])
                _event(run, wait_message, kind="resource", level="warning")
            return None
        run.status = AnalysisRun.Status.PREPARING
        run.status_version += 1
        run.progress = 5
        run.current_step = "正在准备 WDL 与输入"
        run.started_at = now
        run.attempt_count += 1
        run.lease_token = uuid.uuid4()
        run.worker_heartbeat_at = now
        run.lease_expires_at = now + timedelta(
            seconds=settings.ANALYSIS_RUN_LEASE_SECONDS
        )
        run.save(
            update_fields=[
                "status",
                "status_version",
                "progress",
                "current_step",
                "started_at",
                "attempt_count",
                "lease_token",
                "worker_heartbeat_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
        _event(run, "worker 已领取运行，开始准备输入。")
        if run.asset_id:
            run.asset
        if run.revision_id:
            run.revision
        if run.workflow_version_id:
            run.workflow_version
            run.workflow_version.workflow
        if run.tool_version_id:
            run.tool_version
        return run


def _revision_bundle(run: AnalysisRun) -> tuple[dict[str, str], str]:
    if run.workflow_version_id or run.tool_version_id:
        if _canonical_bundle_digest(run.source_bundle) != run.source_digest:
            raise RuntimeError("已发布 Workflow 的运行编译产物摘要不匹配。")
        files = run.source_bundle.get("files")
        entrypoint = run.source_bundle.get("entrypoint")
        if not isinstance(files, dict) or not isinstance(entrypoint, str):
            raise RuntimeError("已发布 Workflow 的固定编译产物不完整。")
        return {
            str(path): str(content)
            for path, content in files.items()
        }, normalize_package_path(entrypoint)
    if run.revision is None or run.asset is None:
        raise RuntimeError("运行没有固定的 WDL 来源。")
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


def _canonical_bundle_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _verify_manifest_files(
    manifest: dict[str, Any] | None,
    root: Path,
    *,
    nested: bool = False,
    checkpoint=None,
    snapshot_budget: ResourceSnapshotBudget | None = None,
) -> None:
    if not manifest:
        return
    if nested:
        for key in ("primary", "control"):
            value = manifest.get(key)
            if isinstance(value, dict):
                _verify_manifest_files(
                    value,
                    root,
                    checkpoint=checkpoint,
                    snapshot_budget=snapshot_budget,
                )
        return
    items = manifest.get("files") or manifest.get("resources") or []
    resolved_root = root.resolve()
    for item in items:
        if checkpoint is not None:
            checkpoint()
        if snapshot_budget is not None:
            snapshot_budget.checkpoint()
        if not isinstance(item, dict):
            raise RuntimeError(
                "受管资源完整性证据过旧，请重新投递任务：manifest item"
            )
        relative = Path(str(item.get("relative_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("受管资源 manifest 路径无效。")
        path = resolved_root / relative
        if snapshot_budget is None:
            path = path.resolve()
        if snapshot_budget is not None:
            snapshot_budget.claim_unique(
                f"retry:{resolved_root}:{item.get('kind')}:{relative}"
            )
        try:
            path.relative_to(resolved_root)
        except ValueError as error:
            raise RuntimeError(f"受管资源已不存在：{relative}") from error
        if item.get("kind") == "directory":
            identity = item.get("identity")
            if (
                item.get("verification") != "directory_identity_sha256"
                or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(item.get("digest") or ""),
                )
                or not isinstance(identity, dict)
                or not all(
                    isinstance(identity.get(field), int)
                    and not isinstance(identity.get(field), bool)
                    for field in ("mtime_ns", "ctime_ns", "device", "inode")
                )
            ):
                raise RuntimeError(
                    "受管资源完整性证据过旧，请重新投递任务："
                    f"{relative}"
                )
            expected_identity = [
                identity["mtime_ns"],
                identity["ctime_ns"],
                identity["device"],
                identity["inode"],
            ]
            expected_digest = str(item.get("digest") or "")
            try:
                observed_directory = (
                    snapshot_budget.directory_manifest(
                        path,
                        containment_root=resolved_root,
                    )
                    if snapshot_budget is not None
                    else _directory_manifest(path, checkpoint=checkpoint)
                )
            except ResourceSnapshotBudgetError:
                raise
            except (OSError, ValueError) as error:
                raise RuntimeError(
                    f"受管资源目录无法校验：{relative}"
                ) from error
            actual_identity = [
                observed_directory["identity"]["mtime_ns"],
                observed_directory["identity"]["ctime_ns"],
                observed_directory["identity"]["device"],
                observed_directory["identity"]["inode"],
            ]
            if actual_identity != expected_identity:
                raise RuntimeError(f"受管资源目录在排队后发生变化：{relative}")
            actual_digest = observed_directory["digest"]
            if actual_digest != expected_digest:
                raise RuntimeError(f"受管资源目录校验和不匹配：{relative}")
            catalog_identity_digest = str(
                item.get("catalog_identity_digest") or ""
            )
            normalized_catalog = (
                catalog_identity_digest
                if catalog_identity_digest.startswith("sha256:")
                else f"sha256:{catalog_identity_digest}"
            )
            if catalog_identity_digest and actual_digest != normalized_catalog:
                raise RuntimeError(
                    f"受管资源与目录声明的身份摘要不匹配：{relative}"
                )
            continue
        expected_sha256 = str(item.get("sha256") or "")
        catalog_sha256 = str(item.get("catalog_sha256") or "")
        has_trusted_digest = bool(expected_sha256 or catalog_sha256)
        identity_fields = ("size", "mtime_ns", "device", "inode")
        if (
            not all(
                isinstance(item.get(field), int)
                and not isinstance(item.get(field), bool)
                for field in identity_fields
            )
            or (
                not has_trusted_digest
                and (
                    item.get("verification") != "identity_v2"
                    or not isinstance(item.get("ctime_ns"), int)
                    or isinstance(item.get("ctime_ns"), bool)
                )
            )
        ):
            raise RuntimeError(
                "受管资源完整性证据过旧，请重新投递任务："
                f"{relative}"
            )
        expected = [
            item["size"],
            item["mtime_ns"],
            item["device"],
            item["inode"],
        ]
        try:
            if snapshot_budget is not None:
                observed_file_identity = snapshot_budget.file_identity(
                    path,
                    containment_root=resolved_root,
                )
            else:
                stat = path.stat()
                if not stat_module.S_ISREG(stat.st_mode):
                    raise RuntimeError(f"受管资源不再是普通文件：{relative}")
                observed_file_identity = {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "ctime_ns": stat.st_ctime_ns,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                }
        except (OSError, ValueError) as error:
            raise RuntimeError(f"受管资源已不存在：{relative}") from error
        actual = [
            observed_file_identity["size"],
            observed_file_identity["mtime_ns"],
            observed_file_identity["device"],
            observed_file_identity["inode"],
        ]
        if item.get("ctime_ns") is not None:
            expected.append(item["ctime_ns"])
            actual.append(observed_file_identity["ctime_ns"])
        if actual != expected:
            raise RuntimeError(f"受管资源在排队后发生变化：{relative}")
        if has_trusted_digest:
            try:
                actual_sha256 = (
                    snapshot_budget.file_digest(
                        path,
                        expected_identity=observed_file_identity,
                        containment_root=resolved_root,
                    )
                    if snapshot_budget is not None
                    else _sha256(
                        path,
                        checkpoint=checkpoint,
                        max_bytes=int(
                            settings.ANALYSIS_MANAGED_FILE_CHECKSUM_MAX_BYTES
                        ),
                        containment_root=resolved_root,
                    )
                )
            except ResourceSnapshotBudgetError:
                raise
            except (OSError, ValueError) as error:
                raise RuntimeError(f"受管文件无法稳定校验：{relative}") from error
            normalized_expected = (
                expected_sha256
                if expected_sha256.startswith("sha256:")
                else f"sha256:{expected_sha256}"
            )
            normalized_catalog = (
                catalog_sha256
                if catalog_sha256.startswith("sha256:")
                else f"sha256:{catalog_sha256}"
            )
            if expected_sha256 and actual_sha256 != normalized_expected:
                raise RuntimeError(f"受管资源校验和不匹配：{relative}")
            if catalog_sha256 and actual_sha256 != normalized_catalog:
                raise RuntimeError(f"受管资源与目录声明的校验和不匹配：{relative}")


def _verify_run_resource_manifests(
    run: AnalysisRun,
    *,
    checkpoint=None,
    snapshot_budget: ResourceSnapshotBudget | None = None,
) -> None:
    payload = run.request_payload
    object_manifest_items(payload.get("input_resource_manifest"))
    _verify_manifest_files(
        payload.get("input_resource_manifest"),
        Path(settings.ANALYSIS_RAWDATA_ROOT),
        nested="primary" in (payload.get("input_resource_manifest") or {}),
        checkpoint=checkpoint,
        snapshot_budget=snapshot_budget,
    )
    for key in (
        "database_resource_manifest",
        "reference_resource_manifest",
        "panel_resource_manifest",
    ):
        _verify_manifest_files(
            payload.get(key),
            Path(settings.ANALYSIS_DATABASE_ROOT),
            checkpoint=checkpoint,
            snapshot_budget=snapshot_budget,
        )


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


def _read_result_json(result_path: Path, *, checkpoint=None) -> Any:
    max_bytes = max(
        1,
        int(getattr(settings, "ANALYSIS_RESULT_JSON_MAX_BYTES", 64 * 1024 * 1024)),
    )
    try:
        with _open_regular_readonly(result_path) as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > max_bytes:
                raise RuntimeError(
                    "miniwdl result.json 超过安全上限"
                    f"（{max_bytes} 字节）。"
                )
            remaining = before.st_size
            chunks: list[bytes] = []
            while remaining:
                if checkpoint is not None:
                    checkpoint()
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("miniwdl result.json 在读取期间被截断。")
                chunks.append(chunk)
                remaining -= len(chunk)
            if handle.read(1):
                raise RuntimeError("miniwdl result.json 在读取期间增长。")
            after = os.fstat(handle.fileno())
            current = os.stat(result_path, follow_symlinks=False)
    except RuntimeError:
        raise
    except (OSError, ValueError) as error:
        raise RuntimeError("miniwdl result.json 无法安全读取。") from error
    if (
        _file_identity(before) != _file_identity(after)
        or _file_identity(after) != _file_identity(current)
    ):
        raise RuntimeError("miniwdl result.json 在读取期间发生变化。")
    try:
        def finite_float(raw: str) -> float:
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("result.json 包含非有限数值。")
            return value

        def reject_constant(raw: str) -> None:
            raise ValueError(f"result.json 包含非标准数值：{raw}")

        return json.loads(
            b"".join(chunks).decode("utf-8"),
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("miniwdl result.json 不是有效 JSON。") from error


def _result_error(result_path: Path, fallback: str) -> str:
    if not result_path.is_file():
        return fallback
    try:
        result = _read_result_json(result_path)
    except RuntimeError:
        return fallback

    def message_from(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("message", "cause", "error"):
                message = message_from(value.get(key))
                if message:
                    return message
            return ""
        if isinstance(value, (str, int, float)):
            return str(value)
        return ""

    message = message_from(result)
    if message:
        return message
    return fallback


def _is_infrastructure_error(message: str, log_path: Path) -> bool:
    details = message
    try:
        with log_path.open(encoding="utf-8", errors="replace") as handle:
            details += "\n" + handle.read()[-20000:]
    except OSError:
        pass
    lowered = details.lower()
    return any(pattern in lowered for pattern in INFRASTRUCTURE_ERROR_PATTERNS)


def _failure_metadata(message: str) -> dict[str, Any]:
    lowered = message.lower()
    if any(
        marker in message
        for marker in (
            "资源快照超过请求时间上限",
            "目录快照超过时间上限",
            "目录快照校验正忙",
        )
    ):
        return {
            "code": "ANALYSIS_RESOURCE_VERIFICATION_TIMEOUT",
            "category": "infrastructure",
            "retryable": True,
        }
    if "完整性证据过旧" in message:
        return {
            "code": "ANALYSIS_RESOURCE_MANIFEST_OUTDATED",
            "category": "resource",
            "retryable": False,
        }
    if "受管资源" in message or "resource" in lowered and "changed" in lowered:
        return {
            "code": "ANALYSIS_RESOURCE_CHANGED",
            "category": "resource",
            "retryable": False,
        }
    if any(pattern in lowered for pattern in INFRASTRUCTURE_ERROR_PATTERNS):
        return {
            "code": "ANALYSIS_INFRASTRUCTURE_ERROR",
            "category": "infrastructure",
            "retryable": True,
        }
    if "check json input" in lowered or "unknown input/output" in lowered:
        return {
            "code": "ANALYSIS_WORKFLOW_INPUT_ERROR",
            "category": "workflow",
            "retryable": False,
        }
    if "file not found" in lowered or "no such file" in lowered:
        return {
            "code": "ANALYSIS_RESOURCE_MISSING",
            "category": "resource",
            "retryable": False,
        }
    return {
        "code": "ANALYSIS_TASK_FAILED",
        "category": "application",
        "retryable": False,
    }


def _attempt_paths(run_directory: Path, attempt: int) -> dict[str, Path]:
    attempt_directory = run_directory / f"attempt-{attempt}"
    attempt_directory.mkdir()
    work_directory = attempt_directory / "work"
    work_directory.mkdir()
    return {
        "directory": attempt_directory,
        "work": work_directory,
        "result": attempt_directory / "result.json",
        "stdout": attempt_directory / "miniwdl.stdout.log",
        "stderr": attempt_directory / "miniwdl.log",
    }


def execute_analysis_run(
    run: AnalysisRun,
    heartbeat: _LeaseHeartbeat | None = None,
) -> None:
    _require_trusted_analysis_source(run)
    executable = shutil.which("miniwdl")
    if executable is None:
        raise RuntimeError("analysis-worker 中没有 miniwdl 可执行文件。")
    root = Path(settings.ANALYSIS_RUN_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)

    def resource_checkpoint() -> None:
        if heartbeat is not None and heartbeat.lease_lost.is_set():
            raise AnalysisRunLeaseLost(
                f"analysis run {run.id} lease was lost during resource verification"
            )

    _verify_run_resource_manifests(
        run,
        checkpoint=resource_checkpoint,
        snapshot_budget=ResourceSnapshotBudget(
            deadline_seconds=settings.ANALYSIS_WORKER_RESOURCE_MANIFEST_TIMEOUT_SECONDS,
            checkpoint=resource_checkpoint,
        ),
    )
    staged_object_count = stage_run_object_inputs(
        run,
        checkpoint=resource_checkpoint,
    )
    if staged_object_count:
        _verify_run_resource_manifests(
            run,
            checkpoint=resource_checkpoint,
            snapshot_budget=ResourceSnapshotBudget(
                deadline_seconds=settings.ANALYSIS_WORKER_RESOURCE_MANIFEST_TIMEOUT_SECONDS,
                checkpoint=resource_checkpoint,
            ),
        )
        _event(
            run,
            f"已校验并固定 {staged_object_count} 个对象存储输入。",
            kind="input",
            details={"object_count": staged_object_count},
        )
    run_directory = root / str(run.id)
    _update_run(run, work_directory=str(run_directory))
    run_directory.mkdir(parents=False, exist_ok=False)
    input_path = run_directory / "inputs.json"
    request_path = run_directory / "request.json"
    current_stdout_path = run_directory / "miniwdl.stdout.log"
    current_stderr_path = run_directory / "miniwdl.log"

    entrypoint = _materialize_source(run, run_directory)
    _write_json(input_path, run.input_values)
    _write_json(
        request_path,
        {
            "run_id": str(run.id),
            "source": (
                {
                    "kind": "workflow_version",
                    "slug": run.workflow_version.workflow.slug,
                    "version": run.workflow_version.version,
                    "digest": run.source_digest,
                }
                if run.workflow_version_id
                else {
                    "kind": "tool_version",
                    "tool_id": run.tool_version.tool_id,
                    "version": run.tool_version.version,
                    "digest": run.tool_version.digest,
                }
                if run.tool_version_id
                else {
                    "kind": "wdl_asset",
                    "slug": run.asset.slug,
                    "revision": run.revision.version,
                    "digest": run.revision.digest,
                }
            ),
            "workflow_name": run.workflow_name,
            "sample_id": run.sample_id,
            "created_at": run.created_at,
            "infrastructure_retries": max(
                0, int(settings.ANALYSIS_INFRASTRUCTURE_RETRIES)
            ),
        },
    )
    if staged_object_count:
        verify_run_object_inputs(run, checkpoint=resource_checkpoint)
    _update_run(
        run,
        status=AnalysisRun.Status.RUNNING,
        progress=12,
        current_step="miniwdl 正在校验流程",
    )
    source_name = (
        f"{run.workflow_version.name} v{run.workflow_version.version}"
        if run.workflow_version_id
        else f"{run.tool_version.name} v{run.tool_version.version}"
        if run.tool_version_id
        else f"{run.asset.name} v{run.revision.version}"
    )
    _event(run, f"已固定 {source_name}，启动 miniwdl。")

    total_calls = 1
    if run.workflow_version_id or run.tool_version_id:
        total_calls = max(1, int(run.source_bundle.get("call_count", 1)))
    else:
        workflows = run.revision.analysis.get("workflows", [])
        if workflows:
            total_calls = max(1, int(workflows[0].get("structure", {}).get("call_count", 1)))
    retry_count = max(0, int(settings.ANALYSIS_INFRASTRUCTURE_RETRIES))
    result: dict[str, Any] | None = None
    failure_message = ""
    for attempt in range(1, retry_count + 2):
        if attempt > 1 and staged_object_count:
            verify_run_object_inputs(run, checkpoint=resource_checkpoint)
        paths = _attempt_paths(run_directory, attempt)
        arguments = [
            executable,
            "run",
            "--dir",
            f"{paths['work']}{os.sep}.",
            "--error-json",
            "--no-color",
            "--log-json",
            "--no-outside-imports",
            "--as-me",
            "-o",
            str(paths["result"]),
            "-i",
            str(input_path),
            str(entrypoint),
        ]
        completed_calls: set[str] = set()
        event_count = 0
        with paths["stdout"].open("w", encoding="utf-8") as stdout_handle, paths[
            "stderr"
        ].open("w", encoding="utf-8") as stderr_handle, current_stderr_path.open(
            "w", encoding="utf-8"
        ) as current_stderr_handle:
            process = subprocess.Popen(
                arguments,
                cwd=run_directory,
                stdout=stdout_handle,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            if heartbeat is not None:
                heartbeat.watch(process)
            try:
                assert process.stderr is not None
                for line in process.stderr:
                    stderr_handle.write(line)
                    stderr_handle.flush()
                    current_stderr_handle.write(line)
                    current_stderr_handle.flush()
                    message, level, details = _log_message(line)
                    if not message:
                        continue
                    lowered = message.lower()
                    call_name = str(
                        details.get("call")
                        or details.get("job")
                        or details.get("task")
                        or ""
                    )
                    if call_name and re.search(
                        r"\b(done|complete|succeeded)\b", lowered
                    ):
                        completed_calls.add(call_name)
                    progress = min(92, 15 + int(75 * len(completed_calls) / total_calls))
                    significant = (
                        level in {"warning", "error", "critical"}
                        or call_name
                        or any(
                            word in lowered
                            for word in ("workflow", "container", "task", "done", "fail")
                        )
                    )
                    if significant and event_count < 500:
                        event_count += 1
                        _event(
                            run,
                            message,
                            kind="miniwdl",
                            level="error" if level in {"error", "critical"} else level,
                            details={**details, "attempt": attempt},
                        )
                        _update_run(
                            run,
                            progress=progress,
                            current_step=(call_name or message)[:256],
                        )
                exit_code = process.wait()
            finally:
                _terminate_process_group(process)
                if heartbeat is not None:
                    heartbeat.unwatch(process)
            if heartbeat is not None and heartbeat.lease_lost.is_set():
                raise AnalysisRunLeaseLost(
                    f"analysis run {run.id} lease is no longer active"
                )
        shutil.copyfile(paths["stdout"], current_stdout_path)

        if exit_code == 0:
            if not paths["result"].is_file():
                raise RuntimeError("miniwdl 已退出，但没有生成 result.json。")
            parsed_result = _read_result_json(
                paths["result"],
                checkpoint=resource_checkpoint,
            )
            if not isinstance(parsed_result, dict):
                raise RuntimeError("miniwdl result.json 不是 JSON object。")
            result = parsed_result
            break

        failure_message = _result_error(
            paths["result"],
            f"miniwdl 运行失败（exit {exit_code}），请查看运行日志。",
        )
        infrastructure_failure = _is_infrastructure_error(
            failure_message, paths["stderr"]
        )
        if not infrastructure_failure or attempt > retry_count:
            if infrastructure_failure:
                failure_message = f"执行环境连接中断：{failure_message}"
            break
        _event(
            run,
            f"执行环境连接中断，{int(settings.ANALYSIS_INFRASTRUCTURE_RETRY_DELAY_SECONDS):g} 秒后自动重试（{attempt}/{retry_count}）。",
            kind="infrastructure",
            level="warning",
            details={"attempt": attempt, "max_retries": retry_count},
        )
        _update_run(
            run,
            current_step=f"执行环境恢复中，准备自动重试（{attempt}/{retry_count}）",
        )
        time.sleep(max(0.0, float(settings.ANALYSIS_INFRASTRUCTURE_RETRY_DELAY_SECONDS)))

    if result is None:
        failure = _failure_metadata(failure_message)
        _update_run(
            run,
            status=AnalysisRun.Status.FAILED,
            progress=100,
            current_step="运行失败",
            error=failure_message[:8000],
            error_code=failure["code"],
            error_category=failure["category"],
            error_retryable=failure["retryable"],
            error_details={},
            output_status=AnalysisRun.OutputStatus.UNAVAILABLE,
            finished_at=timezone.now(),
            lease_token=None,
            worker_heartbeat_at=None,
            lease_expires_at=None,
        )
        _event(run, failure_message, level="error")
        return
    output_manifest = {}
    output_status = AnalysisRun.OutputStatus.COMPLETE
    output_error = None

    def output_snapshot_checkpoint() -> None:
        if heartbeat is not None and heartbeat.lease_lost.is_set():
            raise AnalysisRunLeaseLost(
                f"analysis run {run.id} lease is no longer active"
            )

    output_manifest, output_status, output_error = build_output_manifest(
        run,
        result,
        checkpoint=output_snapshot_checkpoint,
    )
    current_step = (
        "工具测试完成"
        if run.run_kind == AnalysisRun.Kind.TOOL_TEST
        else "分析完成"
    )
    if output_error:
        current_step = "执行完成，但输出清单不完整"
    _update_run(
        run,
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        current_step=current_step,
        outputs=result,
        output_manifest=output_manifest,
        output_status=output_status,
        error="" if output_error is None else "输出清单不完整。",
        error_code=output_error["code"] if output_error else "",
        error_category=output_error["category"] if output_error else "",
        error_retryable=output_error["retryable"] if output_error else False,
        error_details=output_error["details"] if output_error else {},
        finished_at=timezone.now(),
        lease_token=None,
        worker_heartbeat_at=None,
        lease_expires_at=None,
    )
    if output_error:
        _event(
            run,
            "WDL 执行成功，但输出清单不完整。",
            kind="output",
            level="error",
            details=output_error["details"],
        )
    else:
        _event(
            run,
            (
                "工具测试完成，结果文件已就绪。"
                if run.run_kind == AnalysisRun.Kind.TOOL_TEST
                else "分析完成，结果文件已就绪。"
            ),
        )


def process_analysis_run(run: AnalysisRun) -> None:
    heartbeat = _LeaseHeartbeat(run)
    try:
        with heartbeat:
            execute_analysis_run(run, heartbeat)
    except AnalysisRunLeaseLost:
        heartbeat.cleanup_services()
        _finalize_cancelled_run(run.pk, run.lease_token)
        return
    except Exception as error:
        try:
            failure = (
                {
                    "code": error.code,
                    "category": error.category,
                    "retryable": error.retryable,
                }
                if isinstance(
                    error,
                    (AnalysisProductTrustError, WorkflowPackageTrustError),
                )
                else
                {
                    "code": error.code,
                    "category": error.category,
                    "retryable": error.retryable,
                }
                if isinstance(error, ObjectInputError)
                else _failure_metadata(str(error))
            )
            _update_run(
                run,
                status=AnalysisRun.Status.FAILED,
                progress=100,
                current_step="运行失败",
                error=str(error)[:8000],
                error_code=failure["code"],
                error_category=failure["category"],
                error_retryable=failure["retryable"],
                error_details=(error.details if isinstance(error, ObjectInputError) else {}),
                output_status=AnalysisRun.OutputStatus.UNAVAILABLE,
                finished_at=timezone.now(),
                lease_token=None,
                worker_heartbeat_at=None,
                lease_expires_at=None,
            )
            _event(run, str(error), level="error")
        except AnalysisRunLeaseLost:
            return

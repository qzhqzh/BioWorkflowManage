from __future__ import annotations

import json
import os
import queue
import signal
import threading
import time
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from botocore.awsrequest import AWSHTTPConnection
from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from workflows import object_input_gc
from workflows.analysis_runtime import process_analysis_run
from workflows.models import (
    AnalysisRun,
    InputStagingCoordinator,
    InputStagingLease,
    WorkflowDocument,
    WorkflowVersion,
)
from workflows.object_input_gc import (
    ObjectInputCacheGCError,
    garbage_collect_object_input_cache,
)
from workflows.object_inputs import (
    _inspect_object_reference_metadata,
    _run_object_head_worker,
    lock_input_staging_coordinator_for_manifest,
    object_manifest_items,
    _pinned_connection_class,
    _reference,
    _request_parameters,
    _target_path,
    _try_claim_staging_lease,
    _validate_endpoint,
    ObjectInputError,
    ObjectStorageProfile,
    verify_run_object_inputs,
)


pytestmark = pytest.mark.usefixtures("auth_disabled")


def _run(name: str) -> AnalysisRun:
    workflow = WorkflowDocument.objects.create(
        slug=f"workflow-{name}",
        name=name,
        kind=WorkflowDocument.Kind.WORKFLOW,
    )
    version = WorkflowVersion.objects.create(
        workflow=workflow,
        version=1,
        name=name,
        semantic_digest="sha256:" + "1" * 64,
        workflow_graph={"id": name},
        compiled_bundle={},
        compiled_digest="sha256:" + "2" * 64,
    )
    return AnalysisRun.objects.create(
        workflow_version=version,
        workflow_name=name,
        sample_id=name,
        status=AnalysisRun.Status.PREPARING,
        lease_token=uuid.uuid4(),
    )


def _profile(**values) -> ObjectStorageProfile:
    defaults = {
        "name": "test",
        "endpoint_url": "https://objects.example.test",
        "region": "us-east-1",
        "allowed_buckets": ("inputs",),
        "client_grants": {"okb": {"inputs": ("incoming/",)}},
        "access_key_id": "access-key-marker",
        "secret_access_key": "secret-key-marker",
        "session_token": "",
        "allow_http": False,
        "allow_private_network": False,
        "allowed_networks": (),
        "expected_bucket_owner": "",
    }
    defaults.update(values)
    return ObjectStorageProfile(**defaults)


def _cached_object(
    staging_root: Path,
    *,
    digest_character: str,
    content: bytes = b"cached-object",
    age_days: int = 60,
) -> tuple[dict, Path]:
    digest_value = digest_character * 64
    relative_path = f"sha256/{digest_value[:2]}/{digest_value}.fastq.gz"
    target = staging_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    target.chmod(0o444)
    modified_at = timezone.now() - timedelta(days=age_days)
    os.utime(target, (modified_at.timestamp(), modified_at.timestamp()))
    item = {
        "reference_type": "s3_object",
        "input": f"read-{digest_character}",
        "semantic_type": "core.file.unknown",
        "authorized_client_id": "okb",
        "input_sequence": 0,
        "kind": "file",
        "profile": "test",
        "bucket": "inputs",
        "key": "incoming/read.fastq.gz",
        "version_id": f"version-{digest_character}",
        "etag": f"etag-{digest_character}",
        "size": len(content),
        "sha256": f"sha256:{digest_value}",
        "verification": "head+conditional-get+sha256",
        "staging_relative_path": relative_path,
    }
    return {"schema_version": 2, "files": [], "objects": [item]}, target


def _head_worker_script(tmp_path: Path, payload: dict | None) -> Path:
    worker = tmp_path / "object_head_test_worker.py"
    if payload is None:
        source = "import time\nwhile True:\n    time.sleep(60)\n"
    else:
        response = json.dumps(payload, separators=(",", ":"))
        source = (
            "import sys\n"
            "sys.stdin.buffer.readline()\n"
            f"sys.stdout.write({(response + chr(10))!r})\n"
            "sys.stdout.flush()\n"
        )
    worker.write_text(source, encoding="utf-8")
    return worker


def test_object_endpoint_blocks_link_local_and_unapproved_private_addresses():
    link_local = [(2, 1, 6, "", ("169.254.169.254", 443))]
    private = [(2, 1, 6, "", ("10.20.1.5", 443))]
    with (
        patch("workflows.object_inputs.socket.getaddrinfo", return_value=link_local),
        pytest.raises(ObjectInputError) as link_local_error,
    ):
        _validate_endpoint(_profile(allow_private_network=True))
    assert link_local_error.value.code == "OBJECT_INPUT_ENDPOINT_FORBIDDEN"

    loopback = [(2, 1, 6, "", ("127.0.0.1", 443))]
    with (
        patch("workflows.object_inputs.socket.getaddrinfo", return_value=loopback),
        pytest.raises(ObjectInputError) as loopback_error,
    ):
        _validate_endpoint(_profile(allow_private_network=True))
    assert loopback_error.value.code == "OBJECT_INPUT_ENDPOINT_FORBIDDEN"

    with (
        patch("workflows.object_inputs.socket.getaddrinfo", return_value=private),
        pytest.raises(ObjectInputError) as private_error,
    ):
        _validate_endpoint(_profile())
    assert private_error.value.code == "OBJECT_INPUT_ENDPOINT_FORBIDDEN"

    site_local = [(10, 1, 6, "", ("fec0::1", 443, 0, 0))]
    with (
        patch("workflows.object_inputs.socket.getaddrinfo", return_value=site_local),
        pytest.raises(ObjectInputError) as site_local_error,
    ):
        _validate_endpoint(_profile(allow_private_network=True))
    assert site_local_error.value.code == "OBJECT_INPUT_ENDPOINT_FORBIDDEN"


def test_object_reference_rejects_surrogate_key_and_quotes_if_match():
    reference = {
        "type": "s3_object",
        "profile": "test",
        "bucket": "inputs",
        "key": "incoming/read.fastq.gz",
        "etag": '"abc123"',
        "size": 1,
        "sha256": "sha256:" + "0" * 64,
    }
    normalized = _reference(reference, input_name="read")
    parameters = _request_parameters(normalized, _profile(), conditional=True)
    assert normalized["etag"] == "abc123"
    assert parameters["IfMatch"] == '"abc123"'

    reference["key"] = "bad" + chr(0xD800)
    with pytest.raises(ObjectInputError) as caught:
        _reference(reference, input_name="read")
    assert caught.value.code == "OBJECT_INPUT_REFERENCE_INVALID"


def test_object_if_match_reaches_botocore_wire_with_quotes():
    class StopRequestError(Exception):
        pass

    reference = {
        "bucket": "inputs",
        "key": "incoming/read.fastq.gz",
        "version_id": "version-1",
        "etag": "abc123",
    }
    seen_headers = {}
    client = boto3.client(
        "s3",
        endpoint_url="http://objects.example.test",
        region_name="us-east-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
    )

    def capture(request, **_kwargs):
        seen_headers.update(request.headers)
        raise StopRequestError

    client.meta.events.register("before-send.s3.GetObject", capture)
    try:
        with pytest.raises(StopRequestError):
            client.get_object(
                **_request_parameters(reference, _profile(), conditional=True)
            )
    finally:
        client.close()

    assert seen_headers["If-Match"] == b'"abc123"'


def test_object_client_configuration_errors_use_stable_envelope():
    reference = _reference({
        "type": "s3_object",
        "profile": "test",
        "bucket": "inputs",
        "key": "incoming/read.fastq.gz",
        "version_id": "version-1",
        "etag": "abc123",
        "size": 1,
        "sha256": "sha256:" + "0" * 64,
    }, input_name="read")
    with (
        patch(
            "workflows.object_inputs._load_profile",
            return_value=_profile(region="not a valid region"),
        ),
        patch(
            "workflows.object_inputs._validate_endpoint",
            return_value=("203.0.113.10",),
        ),
        pytest.raises(ObjectInputError) as caught,
    ):
        _inspect_object_reference_metadata(
            reference,
            client_id="okb",
        )

    assert caught.value.code == "OBJECT_INPUT_UNAVAILABLE"
    assert caught.value.http_status == 503


def test_object_connections_use_only_pinned_addresses():
    connection_class = _pinned_connection_class(
        AWSHTTPConnection,
        ("203.0.113.10",),
    )
    connection = connection_class("objects.example.test", 80)
    sentinel = object()
    with patch(
        "workflows.object_inputs.urllib3_connection.create_connection",
        return_value=sentinel,
    ) as create_connection:
        assert connection._new_conn() is sentinel
    assert create_connection.call_args.args[0] == ("203.0.113.10", 80)


def test_object_head_timeout_terminates_process_and_releases_slot(
    monkeypatch,
    tmp_path,
):
    from workflows import object_inputs

    monkeypatch.setattr(object_inputs, "_HEAD_CALL_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        object_inputs,
        "_OBJECT_HEAD_WORKER",
        _head_worker_script(tmp_path, None),
    )
    started = time.monotonic()
    with pytest.raises(ObjectInputError) as timed_out:
        _run_object_head_worker({}, client_id=None, timeout=0.01)
    assert timed_out.value.code == "OBJECT_INPUT_HEAD_TIMEOUT"
    assert time.monotonic() - started < 1
    ready = {
        "ok": True,
        "metadata": {
            "ContentLength": 1,
            "DeleteMarker": False,
            "ETag": '"abc123"',
            "VersionId": "version-1",
        },
    }
    monkeypatch.setattr(
        object_inputs,
        "_OBJECT_HEAD_WORKER",
        _head_worker_script(tmp_path, ready),
    )
    assert _run_object_head_worker({}, client_id=None, timeout=0.5) == ready["metadata"]


def test_object_head_worker_preserves_stable_errors(monkeypatch, tmp_path):
    from workflows import object_inputs

    expected = {
        "ok": False,
        "error": {
            "code": "OBJECT_INPUT_PROFILE_FORBIDDEN",
            "message": "forbidden",
            "retryable": False,
            "details": {"profile": "test"},
            "http_status": 403,
        },
    }
    monkeypatch.setattr(
        object_inputs,
        "_OBJECT_HEAD_WORKER",
        _head_worker_script(tmp_path, expected),
    )
    with pytest.raises(ObjectInputError) as caught:
        _run_object_head_worker({}, client_id=None, timeout=0.5)

    assert caught.value.code == expected["error"]["code"]
    assert str(caught.value) == expected["error"]["message"]
    assert caught.value.retryable is False
    assert caught.value.details == expected["error"]["details"]
    assert caught.value.http_status == expected["error"]["http_status"]


def test_object_head_worker_runs_from_request_thread(monkeypatch, tmp_path):
    from workflows import object_inputs

    response = {
        "ok": True,
        "metadata": {
            "ContentLength": 1,
            "DeleteMarker": False,
            "ETag": '"abc123"',
            "VersionId": "version-1",
        },
    }
    monkeypatch.setattr(
        object_inputs,
        "_OBJECT_HEAD_WORKER",
        _head_worker_script(tmp_path, response),
    )
    results = queue.Queue()

    def invoke():
        try:
            results.put((True, _run_object_head_worker({}, client_id=None, timeout=1)))
        except BaseException as error:
            results.put((False, error))

    request_thread = threading.Thread(target=invoke)
    request_thread.start()
    request_thread.join(timeout=2)

    assert not request_thread.is_alive()
    succeeded, value = results.get_nowait()
    assert succeeded is True
    assert value == response["metadata"]


@pytest.mark.parametrize("failure", [OSError("full"), RuntimeError("unavailable")])
def test_object_head_worker_start_failure_is_stable(monkeypatch, failure):
    from workflows import object_inputs

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(object_inputs, "_HEAD_CALL_SLOTS", slots)
    with (
        patch("workflows.object_inputs.subprocess.Popen", side_effect=failure),
        pytest.raises(ObjectInputError) as caught,
    ):
        _run_object_head_worker({}, client_id=None, timeout=0.5)

    assert caught.value.code == "OBJECT_INPUT_UNAVAILABLE"
    assert caught.value.retryable is True
    assert caught.value.http_status == 503
    assert slots.acquire(blocking=False) is True
    slots.release()


def test_object_head_worker_enforces_own_deadline():
    from workflows.object_head_worker import _ObjectHeadDeadline, _arm_deadline

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        _arm_deadline(0.1)
        with pytest.raises(_ObjectHeadDeadline):
            time.sleep(1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def test_object_head_worker_parent_death_race_is_fail_closed(monkeypatch):
    from workflows import object_head_worker

    class Libc:
        def __init__(self):
            self.calls = []

        def prctl(self, *values):
            self.calls.append(values)
            return 0

    libc = Libc()
    kills = []
    monkeypatch.setattr(object_head_worker.sys, "platform", "linux")
    monkeypatch.setattr(object_head_worker.ctypes, "CDLL", lambda *_args, **_kwargs: libc)
    monkeypatch.setattr(object_head_worker.os, "getppid", lambda: 222)
    monkeypatch.setattr(object_head_worker.os, "getpid", lambda: 333)
    monkeypatch.setattr(
        object_head_worker.os,
        "kill",
        lambda pid, target_signal: kills.append((pid, target_signal)),
    )

    object_head_worker._arm_parent_death_signal(111)

    assert libc.calls == [(1, signal.SIGKILL, 0, 0, 0)]
    assert kills == [(333, signal.SIGKILL)]


def test_object_staging_rejects_symlink_ancestor(settings, tmp_path):
    staging = tmp_path / "staging"
    outside = tmp_path / "outside"
    staging.mkdir()
    outside.mkdir()
    (staging / "sha256").symlink_to(outside, target_is_directory=True)
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    reference = {
        "sha256": "sha256:" + "a" * 64,
        "key": "sample.fastq.gz",
    }
    with pytest.raises(ObjectInputError) as caught:
        _target_path(reference)
    assert caught.value.code == "OBJECT_INPUT_STAGE_IO_ERROR"


def test_object_run_budget_counts_distinct_remote_identities(settings):
    settings.ANALYSIS_OBJECT_STAGE_MAX_RUN_BYTES = 100
    digest = "sha256:" + "a" * 64

    def item(key: str, sequence: int):
        return {
            "reference_type": "s3_object",
            "input": f"read{sequence}",
            "semantic_type": "core.file.unknown",
            "authorized_client_id": "okb",
            "input_sequence": sequence,
            "kind": "file",
            "profile": "test",
            "bucket": "inputs",
            "key": key,
            "version_id": f"version-{sequence}",
            "etag": f"etag-{sequence}",
            "size": 60,
            "sha256": digest,
            "verification": "head+conditional-get+sha256",
            "staging_relative_path": (
                f"sha256/aa/{'a' * 64}.fastq.gz"
            ),
        }

    manifest = {
        "schema_version": 2,
        "objects": [
            item("incoming/a.fastq.gz", 1),
            item("incoming/b.fastq.gz", 2),
        ],
    }

    with pytest.raises(ObjectInputError) as caught:
        object_manifest_items(manifest)

    assert caught.value.code == "OBJECT_INPUT_RUN_TOO_LARGE"


def test_object_execution_recheck_has_hard_timeout(settings, tmp_path, monkeypatch):
    from workflows import object_inputs

    settings.ANALYSIS_INPUT_STAGING_ROOT = tmp_path / "staging"
    settings.ANALYSIS_OBJECT_STAGE_TIMEOUT_SECONDS = 0.01
    settings.ANALYSIS_OBJECT_STAGE_RUN_TIMEOUT_SECONDS = 0.05
    digest = "sha256:" + "a" * 64
    item = {
        "reference_type": "s3_object",
        "input": "read1",
        "semantic_type": "core.file.unknown",
        "authorized_client_id": "okb",
        "input_sequence": 0,
        "kind": "file",
        "profile": "test",
        "bucket": "inputs",
        "key": "incoming/read.fastq.gz",
        "version_id": "version-1",
        "etag": "etag-1",
        "size": 1,
        "sha256": digest,
        "verification": "head+conditional-get+sha256",
        "staging_relative_path": f"sha256/aa/{'a' * 64}.fastq.gz",
    }
    run = type(
        "Run",
        (),
        {
            "request_payload": {
                "input_resource_manifest": {
                    "schema_version": 2,
                    "files": [],
                    "objects": [item],
                }
            }
        },
    )()
    monkeypatch.setattr(
        object_inputs,
        "_hash_target_at",
        lambda *_args, **_kwargs: time.sleep(1),
    )

    started = time.monotonic()
    with pytest.raises(ObjectInputError) as caught:
        verify_run_object_inputs(run)

    assert caught.value.code == "OBJECT_INPUT_STAGE_TIMEOUT"
    assert time.monotonic() - started < 0.5


@pytest.mark.django_db(transaction=True)
def test_object_stage_failure_keeps_stable_run_error_code():
    run = _run("staging-failure")
    failure = ObjectInputError(
        "OBJECT_INPUT_CHANGED",
        "object changed",
        retryable=False,
        details={"input": "read1"},
    )
    with patch(
        "workflows.analysis_runtime.execute_analysis_run",
        side_effect=failure,
    ):
        process_analysis_run(run)
    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.FAILED
    assert run.error_code == "OBJECT_INPUT_CHANGED"
    assert run.error_category == "input"
    assert run.error_retryable is False
    assert run.error_details == {"input": "read1"}


@pytest.mark.django_db(transaction=True)
def test_postgresql_input_staging_slot_is_serialized(settings, tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking is required for this concurrency test")
    settings.ANALYSIS_INPUT_STAGING_ROOT = tmp_path / "staging"
    settings.ANALYSIS_OBJECT_STAGE_MAX_CONCURRENT_RUNS = 1
    settings.ANALYSIS_OBJECT_STAGE_MAX_RESERVED_BYTES = 1024
    settings.ANALYSIS_OBJECT_STAGE_MIN_FREE_BYTES = 0
    InputStagingCoordinator.objects.get_or_create(pk=1)
    runs = [_run("staging-a"), _run("staging-b")]
    barrier = threading.Barrier(2)
    results: queue.Queue[tuple[bool, str]] = queue.Queue()

    def claim(run_id):
        close_old_connections()
        try:
            run = AnalysisRun.objects.get(pk=run_id)
            barrier.wait(timeout=5)
            lease, reason = _try_claim_staging_lease(run, reserved_bytes=100)
            results.put((lease is not None, reason))
        finally:
            close_old_connections()

    workers = [threading.Thread(target=claim, args=(run.pk,)) for run in runs]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    outcomes = sorted(results.get_nowait() for _ in workers)
    assert outcomes == [(False, "busy"), (True, "")]


@pytest.mark.django_db(transaction=True)
def test_staging_claim_cleans_only_inactive_lease_temp_directory(settings, tmp_path):
    staging = tmp_path / "staging"
    stale_id = uuid.uuid4()
    stale_directory = staging / ".leases" / str(stale_id)
    stale_directory.mkdir(parents=True)
    (stale_directory / "orphan.part").write_bytes(b"orphan")
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    settings.ANALYSIS_OBJECT_STAGE_MAX_CONCURRENT_RUNS = 2
    settings.ANALYSIS_OBJECT_STAGE_MAX_RESERVED_BYTES = 1024
    settings.ANALYSIS_OBJECT_STAGE_MIN_FREE_BYTES = 0
    InputStagingCoordinator.objects.get_or_create(pk=1)

    lease, reason = _try_claim_staging_lease(_run("stale-cleanup"), reserved_bytes=1)

    assert lease is not None
    assert reason == ""
    assert not stale_directory.exists()


@pytest.mark.django_db(transaction=True)
def test_object_cache_gc_protects_runs_and_leases_and_is_idempotent(
    settings,
    tmp_path,
):
    staging = tmp_path / "staging"
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    settings.ANALYSIS_OBJECT_STAGE_RETENTION_DAYS = 30
    settings.ANALYSIS_OBJECT_STAGE_GC_MAX_FILES = 100
    settings.ANALYSIS_OBJECT_STAGE_GC_SCAN_MAX_FILES = 100
    InputStagingCoordinator.objects.get_or_create(pk=1)
    now = timezone.now()

    active_manifest, active_target = _cached_object(
        staging,
        digest_character="a",
    )
    expired_manifest, expired_target = _cached_object(
        staging,
        digest_character="b",
    )
    leased_manifest, leased_target = _cached_object(
        staging,
        digest_character="c",
    )
    recent_manifest, recent_target = _cached_object(
        staging,
        digest_character="d",
    )
    _, fresh_target = _cached_object(
        staging,
        digest_character="e",
        age_days=1,
    )

    active_run = _run("gc-active")
    AnalysisRun.objects.filter(pk=active_run.pk).update(
        request_payload={"input_resource_manifest": active_manifest},
    )
    expired_run = _run("gc-expired")
    AnalysisRun.objects.filter(pk=expired_run.pk).update(
        status=AnalysisRun.Status.SUCCEEDED,
        request_payload={"input_resource_manifest": expired_manifest},
        updated_at=now - timedelta(days=60),
    )
    leased_run = _run("gc-leased")
    AnalysisRun.objects.filter(pk=leased_run.pk).update(
        status=AnalysisRun.Status.SUCCEEDED,
        request_payload={"input_resource_manifest": leased_manifest},
        updated_at=now - timedelta(days=60),
    )
    lease = InputStagingLease.objects.create(
        run=leased_run,
        worker_lease_token=leased_run.lease_token,
        reserved_bytes=leased_target.stat().st_size,
        expires_at=now + timedelta(hours=1),
    )
    recent_run = _run("gc-recent")
    AnalysisRun.objects.filter(pk=recent_run.pk).update(
        status=AnalysisRun.Status.SUCCEEDED,
        request_payload={"input_resource_manifest": recent_manifest},
    )
    lease_directory = staging / ".leases" / str(lease.id)
    lease_directory.mkdir(parents=True)
    (lease_directory / "active.part").write_bytes(b"active")

    planned = garbage_collect_object_input_cache(
        all_eligible=True,
        actor="pytest",
    )

    assert planned["mode"] == "dry_run"
    assert planned["selected_files"] == 1
    assert planned["candidates"][0]["relative_path"] == str(
        expired_target.relative_to(staging)
    )
    assert planned["skipped_reasons"] == {
        "active_lease": 1,
        "active_run": 1,
        "recent_terminal_run": 1,
        "retention_period_active": 1,
    }
    assert all(
        target.exists()
        for target in (
            active_target,
            expired_target,
            leased_target,
            recent_target,
            fresh_target,
        )
    )

    applied = garbage_collect_object_input_cache(
        apply=True,
        all_eligible=True,
        actor="pytest",
    )

    assert applied["deleted_files"] == 1
    assert applied["released_bytes"] == len(b"cached-object")
    assert applied["empty_directories_removed"] == 1
    assert not expired_target.exists()
    assert not expired_target.parent.exists()
    assert active_target.exists()
    assert leased_target.exists()
    assert recent_target.exists()
    assert fresh_target.exists()
    assert (lease_directory / "active.part").read_bytes() == b"active"

    repeated = garbage_collect_object_input_cache(
        apply=True,
        all_eligible=True,
        actor="pytest",
    )
    assert repeated["deleted_files"] == 0
    assert repeated["released_bytes"] == 0


@pytest.mark.django_db(transaction=True)
def test_object_cache_cleanup_command_is_dry_run_by_default(settings, tmp_path):
    staging = tmp_path / "staging"
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    settings.ANALYSIS_OBJECT_STAGE_RETENTION_DAYS = 30
    InputStagingCoordinator.objects.get_or_create(pk=1)
    _, target = _cached_object(staging, digest_character="a")
    output = StringIO()

    call_command(
        "cleanup_object_input_cache",
        "--all-eligible",
        "--actor",
        "pytest",
        stdout=output,
    )

    rendered = output.getvalue()
    assert "DRY_RUN no object input cache files were deleted" in rendered
    assert '"selected_files": 1' in rendered
    assert '"mode": "dry_run"' in rendered
    assert target.exists()


@pytest.mark.parametrize(
    ("unsafe_kind", "expected_code"),
    [
        ("unknown", "OBJECT_INPUT_CACHE_GC_UNKNOWN_NODE"),
        ("symlink", "OBJECT_INPUT_CACHE_GC_PATH_UNSAFE"),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_object_cache_gc_fails_closed_on_unknown_or_symlink_nodes(
    settings,
    tmp_path,
    unsafe_kind,
    expected_code,
):
    staging = tmp_path / "staging"
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    settings.ANALYSIS_OBJECT_STAGE_RETENTION_DAYS = 30
    InputStagingCoordinator.objects.get_or_create(pk=1)
    _, safe_target = _cached_object(staging, digest_character="a")
    if unsafe_kind == "unknown":
        (staging / "unexpected").write_text("unknown", encoding="utf-8")
    else:
        unsafe_bucket = staging / "sha256" / "ff"
        unsafe_bucket.mkdir()
        (unsafe_bucket / f"{'f' * 64}.fastq.gz").symlink_to(safe_target)

    with pytest.raises(ObjectInputCacheGCError) as caught:
        garbage_collect_object_input_cache(
            apply=True,
            all_eligible=True,
            actor="pytest",
        )

    assert caught.value.code == expected_code
    assert safe_target.exists()


@pytest.mark.django_db(transaction=True)
def test_object_cache_gc_uses_high_and_low_watermarks(settings, tmp_path):
    staging = tmp_path / "staging"
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    settings.ANALYSIS_OBJECT_STAGE_RETENTION_DAYS = 30
    settings.ANALYSIS_OBJECT_STAGE_GC_HIGH_WATER_PERCENT = 80
    settings.ANALYSIS_OBJECT_STAGE_GC_LOW_WATER_PERCENT = 70
    InputStagingCoordinator.objects.get_or_create(pk=1)
    _cached_object(staging, digest_character="a", content=b"a" * 120)
    _cached_object(staging, digest_character="b", content=b"b" * 120)

    below_high = SimpleNamespace(total=1000, used=799, free=201)
    with patch("workflows.object_input_gc._disk_usage", return_value=below_high):
        skipped = garbage_collect_object_input_cache(actor="pytest")

    assert skipped["watermark_triggered"] is False
    assert skipped["selected_files"] == 0
    assert skipped["skipped_reasons"] == {"below_high_water": 2}

    above_high = SimpleNamespace(total=1000, used=900, free=100)
    with patch("workflows.object_input_gc._disk_usage", return_value=above_high):
        planned = garbage_collect_object_input_cache(actor="pytest")

    assert planned["watermark_triggered"] is True
    assert planned["selected_files"] == 2
    assert planned["selected_bytes"] == 240
    assert planned["projected_used_bytes"] == 660
    assert planned["low_water_reached"] is True


@pytest.mark.django_db(transaction=True)
def test_object_cache_gc_rejects_file_changes_between_scan_and_apply(
    settings,
    tmp_path,
):
    staging = tmp_path / "staging"
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    settings.ANALYSIS_OBJECT_STAGE_RETENTION_DAYS = 30
    InputStagingCoordinator.objects.get_or_create(pk=1)
    _, target = _cached_object(staging, digest_character="a")
    original_validate = object_input_gc._validate_scan_snapshot

    def mutate_then_validate(*args, **kwargs):
        target.chmod(0o644)
        target.write_bytes(b"changed-after-scan")
        target.chmod(0o444)
        return original_validate(*args, **kwargs)

    with (
        patch(
            "workflows.object_input_gc._validate_scan_snapshot",
            side_effect=mutate_then_validate,
        ),
        pytest.raises(ObjectInputCacheGCError) as caught,
    ):
        garbage_collect_object_input_cache(
            apply=True,
            all_eligible=True,
            actor="pytest",
        )

    assert caught.value.code == "OBJECT_INPUT_CACHE_GC_RACE"
    assert target.read_bytes() == b"changed-after-scan"


@pytest.mark.django_db(transaction=True)
def test_postgresql_cache_gc_waits_for_object_backed_run_creation(
    settings,
    tmp_path,
):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking is required for this concurrency test")
    staging = tmp_path / "staging"
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    settings.ANALYSIS_OBJECT_STAGE_RETENTION_DAYS = 30
    InputStagingCoordinator.objects.get_or_create(pk=1)
    manifest, target = _cached_object(staging, digest_character="a")
    source_run = _run("gc-run-creation-source")
    created_uncommitted = threading.Event()
    allow_commit = threading.Event()
    creation_results: queue.Queue[tuple[str, object]] = queue.Queue()
    gc_results: queue.Queue[tuple[str, object]] = queue.Queue()

    def create_object_backed_run():
        close_old_connections()
        try:
            with transaction.atomic():
                version = WorkflowVersion.objects.get(pk=source_run.workflow_version_id)
                lock_input_staging_coordinator_for_manifest(manifest)
                run = AnalysisRun.objects.create(
                    workflow_version=version,
                    workflow_name="gc-concurrent-run",
                    sample_id="gc-concurrent-run",
                    status=AnalysisRun.Status.QUEUED,
                    request_payload={"input_resource_manifest": manifest},
                )
                created_uncommitted.set()
                if not allow_commit.wait(timeout=10):
                    raise AssertionError("timed out waiting to commit run")
            creation_results.put(("ok", run.pk))
        except BaseException as error:
            creation_results.put(("error", error))
        finally:
            connection.close()

    def collect_cache():
        close_old_connections()
        try:
            result = garbage_collect_object_input_cache(
                apply=True,
                all_eligible=True,
                actor="pytest",
            )
            gc_results.put(("ok", result))
        except BaseException as error:
            gc_results.put(("error", error))
        finally:
            connection.close()

    creator = threading.Thread(target=create_object_backed_run)
    collector = threading.Thread(target=collect_cache)
    creator.start()
    try:
        assert created_uncommitted.wait(timeout=5)
        collector.start()
        time.sleep(0.2)
        assert collector.is_alive()
        assert gc_results.empty()
        assert target.exists()
    finally:
        allow_commit.set()
        creator.join(timeout=10)
        if collector.ident is not None:
            collector.join(timeout=10)

    assert not creator.is_alive()
    assert not collector.is_alive()
    creation_status, creation_value = creation_results.get_nowait()
    gc_status, gc_value = gc_results.get_nowait()
    assert creation_status == "ok", creation_value
    assert gc_status == "ok", gc_value
    assert gc_value["deleted_files"] == 0
    assert gc_value["skipped_reasons"]["active_run"] == 1
    assert target.exists()

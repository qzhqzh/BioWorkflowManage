from __future__ import annotations

import json
import queue
import signal
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.awsrequest import AWSHTTPConnection
from django.db import close_old_connections, connection

from workflows.analysis_runtime import process_analysis_run
from workflows.models import (
    AnalysisRun,
    InputStagingCoordinator,
    WorkflowDocument,
    WorkflowVersion,
)
from workflows.object_inputs import (
    _inspect_object_reference_metadata,
    _run_object_head_worker,
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

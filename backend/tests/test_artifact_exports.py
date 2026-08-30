from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.utils import timezone
from jsonschema import Draft202012Validator
from rest_framework.test import APIClient

from workflows import artifact_exports as artifact_exports_module
from workflows.artifact_exports import (
    ARTIFACT_EXPORT_EVENT_TYPE,
    ArtifactExportError,
    acknowledge_artifact_export,
    artifact_export_payload,
    claim_next_artifact_export,
    claim_next_output_cleanup,
    clean_analysis_output,
    create_artifact_export,
    deliver_artifact_export,
    output_cleanup_candidates,
)
from workflows.integration_outputs import build_output_manifest
from workflows.integration_tokens import issue_service_token
from workflows.object_inputs import ObjectInputError
from workflows.models import (
    AnalysisRun,
    ArtifactExport,
    ArtifactExportAttempt,
    IntegrationOutboxEvent,
    ServiceAccount,
    ToolVersion,
    WebhookDelivery,
    WebhookEndpoint,
)


pytestmark = pytest.mark.usefixtures("auth_disabled")


ALL_SCOPES = [
    "analysis:submit",
    "analysis:read",
    "analysis:cancel",
    "analysis:retry",
    "analysis:download",
    "analysis:export",
    "analysis:acknowledge",
    "workflow:read",
    "library:read",
    "task:test",
]


@pytest.fixture
def artifact_workspace(settings, tmp_path):
    runs = tmp_path / "runs"
    destinations = tmp_path / "artifact-exports"
    profiles = tmp_path / "profiles"
    runs.mkdir()
    destinations.mkdir()
    profiles.mkdir()
    settings.ANALYSIS_RUN_ROOT = runs
    settings.ANALYSIS_RUN_EXECUTION_ROOT = runs
    settings.ANALYSIS_ARTIFACT_EXPORT_ROOT = destinations
    settings.ANALYSIS_ARTIFACT_EXPORT_PROFILE_DIR = profiles
    settings.ANALYSIS_ARTIFACT_EXPORT_TIMEOUT_SECONDS = 10
    settings.ANALYSIS_ARTIFACT_EXPORT_LEASE_SECONDS = 60
    settings.ANALYSIS_ARTIFACT_EXPORT_MAX_ATTEMPTS = 3
    settings.ANALYSIS_ARTIFACT_EXPORT_BACKOFF_BASE_SECONDS = 1
    settings.ANALYSIS_ARTIFACT_EXPORT_BACKOFF_MAX_SECONDS = 2
    settings.ANALYSIS_ARTIFACT_EXPORT_CHUNK_BYTES = 64 * 1024
    settings.ANALYSIS_ARTIFACT_EXPORT_MANIFEST_MAX_BYTES = 1024 * 1024
    settings.ANALYSIS_ARTIFACT_RETENTION_MIN_DAYS = 0
    settings.ANALYSIS_ARTIFACT_RETENTION_MAX_DAYS = 365
    settings.ANALYSIS_ARTIFACT_CLEANUP_LEASE_SECONDS = 60
    return runs, destinations, profiles


def _account(*, client_id="mes", scopes=None) -> tuple[ServiceAccount, APIClient]:
    account = ServiceAccount.objects.create(
        client_id=client_id,
        name=client_id.upper(),
        scopes=ALL_SCOPES if scopes is None else scopes,
        created_by="pytest",
    )
    _, raw_token = issue_service_token(account, name="test", actor="pytest")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
    return account, client


def _write_managed_profile(
    profiles: Path,
    account: ServiceAccount,
    *,
    name="customer-results",
) -> None:
    (profiles / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "managed_directory",
                "allowed_clients": [account.client_id],
                "directory": "customers/mes",
                "root_alias": "mes-results",
                "public_uri_prefix": "nas://mes-results",
            }
        ),
        encoding="utf-8",
    )


def _write_s3_profile(
    profiles: Path,
    account: ServiceAccount,
    *,
    name="customer-s3",
) -> tuple[str, str]:
    access_key = "artifact-access-key-not-for-persistence"
    secret_key = "artifact-secret-key-not-for-persistence"
    (profiles / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "s3",
                "allowed_clients": [account.client_id],
                "endpoint_url": "https://objects.example.test",
                "region": "us-east-1",
                "bucket": "delivered-results",
                "prefix": "analysis",
                "access_key_id": access_key,
                "secret_access_key": secret_key,
            }
        ),
        encoding="utf-8",
    )
    return access_key, secret_key


def _run_with_outputs(
    account: ServiceAccount,
    runs: Path,
    *,
    file_values: tuple[bytes, ...] = (b"sample\tvalue\nS001\t1\n",),
    include_value: bool = True,
) -> tuple[AnalysisRun, list[Path]]:
    suffix = uuid.uuid4().hex[:8]
    tool = ToolVersion.objects.create(
        tool_id=f"artifact-export-{suffix}",
        version="1.0.0",
        name="Artifact Export Fixture",
        digest="sha256:" + "1" * 64,
        tool_spec={"runtime": {"memory_gb": 1}},
    )
    contract = [
        {
            "key": f"ExportTask.file{index}",
            "name": f"file{index}",
            "label": f"Result {index}",
            "semantic_type": "report.qc_tsv",
            "wdl_type": "File",
            "required": True,
        }
        for index in range(1, len(file_values) + 1)
    ]
    if include_value:
        contract.append(
            {
                "key": "ExportTask.note",
                "name": "note",
                "label": "Note",
                "semantic_type": "report.note",
                "wdl_type": "String",
                "required": False,
            }
        )
    run = AnalysisRun.objects.create(
        run_kind=AnalysisRun.Kind.TOOL_TEST,
        tool_version=tool,
        service_account=account,
        external_run_id=f"external-{suffix}",
        idempotency_key=f"run-{suffix}",
        workflow_name="ExportTask",
        sample_id="S001",
        actor=f"service:{account.client_id}",
        status=AnalysisRun.Status.SUCCEEDED,
        progress=100,
        finished_at=timezone.now(),
        request_payload={
            "external_ref": {
                "client_id": account.client_id,
                "external_run_id": f"external-{suffix}",
            },
            "integration_output_contract": contract,
        },
    )
    run_directory = runs / str(run.id)
    run_directory.mkdir()
    outputs = {}
    paths = []
    for index, value in enumerate(file_values, start=1):
        path = run_directory / f"result-{index}.tsv"
        path.write_bytes(value)
        paths.append(path)
        outputs[f"ExportTask.file{index}"] = str(path)
    if include_value:
        outputs["ExportTask.note"] = "verified result"
    run.work_directory = str(run_directory)
    result = {"outputs": outputs}
    manifest, output_status, error = build_output_manifest(run, result)
    assert error is None
    run.outputs = result
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(
        update_fields=[
            "work_directory",
            "outputs",
            "output_manifest",
            "output_status",
            "updated_at",
        ]
    )
    return run, paths


def _post_export(client: APIClient, run: AnalysisRun, *, key="export-1"):
    return client.post(
        f"/api/v1/integration/analysis-runs/{run.id}/artifact-exports",
        {
            "target": {"profile": "customer-results"},
            "requires_ack": True,
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


def _validate_openapi_schema(name: str, value) -> None:
    openapi = json.loads(
        (Path(__file__).parents[2] / "schemas" / "integration-openapi-v1.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(
        {
            **openapi,
            "$ref": f"#/components/schemas/{name}",
        }
    ).validate(value)


@pytest.mark.django_db
def test_artifact_export_api_is_scoped_idempotent_and_profile_only(
    artifact_workspace,
):
    runs, _, profiles = artifact_workspace
    account, client = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs)

    created = _post_export(client, run)
    replayed = _post_export(client, run)

    assert created.status_code == 201, created.data
    assert replayed.status_code == 200
    assert replayed.data["id"] == created.data["id"]
    assert created.data["state"] == ArtifactExport.State.PENDING
    assert created.data["requires_ack"] is True
    assert created.data["target"] == {
        "type": "managed_directory",
        "profile": "customer-results",
        "root_alias": "mes-results",
        "public_uri_prefix": "nas://mes-results",
    }
    _validate_openapi_schema("ArtifactExport", created.data)
    conflict = client.post(
        f"/api/v1/integration/analysis-runs/{run.id}/artifact-exports",
        {
            "target": {"profile": "customer-results"},
            "requires_ack": False,
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="export-1",
    )
    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    account.scopes = ["analysis:read"]
    account.save(update_fields=["scopes", "updated_at"])
    forbidden = _post_export(client, run, key="export-2")
    assert forbidden.status_code == 403
    assert forbidden.data["error"]["code"] == "SERVICE_SCOPE_REQUIRED"


@pytest.mark.django_db
def test_managed_export_delivery_webhook_and_acknowledgement(
    artifact_workspace,
):
    runs, destinations, profiles = artifact_workspace
    account, client = _account()
    _write_managed_profile(profiles, account)
    endpoint = WebhookEndpoint.objects.create(
        service_account=account,
        name="artifact-complete",
        url="https://hooks.example.test/artifacts",
        event_types=[ARTIFACT_EXPORT_EVENT_TYPE],
        created_by="pytest",
    )
    run, source_paths = _run_with_outputs(account, runs)
    response = _post_export(client, run)
    export_id = response.data["id"]

    claimed = claim_next_artifact_export()
    assert claimed is not None
    assert str(claimed.id) == export_id
    assert deliver_artifact_export(claimed) is True

    export = ArtifactExport.objects.select_related("run", "retention").get(pk=export_id)
    assert export.state == ArtifactExport.State.SUCCEEDED
    assert export.manifest_digest.startswith("sha256:")
    assert export.manifest["summary"] == {
        "file_count": 1,
        "item_count": 2,
        "total_bytes": source_paths[0].stat().st_size,
    }
    delivered_file = next(
        item for item in export.manifest["items"] if item["kind"] == "file"
    )
    assert delivered_file["semantic_type"] == "report.qc_tsv"
    relative_path = delivered_file["destination"]["relative_path"]
    assert (destinations / relative_path).read_bytes() == source_paths[0].read_bytes()
    export_directory = (destinations / relative_path).parent
    assert export_directory.stat().st_mode & 0o050 == 0o050
    manifest_location = destinations / export.manifest_location["relative_path"]
    assert hashlib.sha256(manifest_location.read_bytes()).hexdigest() == (
        export.manifest_digest.removeprefix("sha256:")
    )

    event = IntegrationOutboxEvent.objects.get(
        event_type=ARTIFACT_EXPORT_EVENT_TYPE,
    )
    assert event.deduplication_key == f"export:{export.id}"
    assert event.payload["data"]["manifest_digest"] == export.manifest_digest
    assert "items" not in event.payload["data"]
    assert len(json.dumps(event.payload)) < 4096
    _validate_openapi_schema("ArtifactExportCompletedEvent", event.payload)
    assert WebhookDelivery.objects.filter(event=event, endpoint=endpoint).count() == 1
    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.SUCCEEDED

    mismatch = client.post(
        f"/api/v1/integration/artifact-exports/{export.id}/acknowledge",
        {
            "manifest_digest": "sha256:" + "0" * 64,
            "external_receipt": "mes-receipt-1",
        },
        format="json",
    )
    assert mismatch.status_code == 409
    assert mismatch.data["error"]["code"] == "ARTIFACT_EXPORT_ACK_DIGEST_MISMATCH"
    acknowledged = client.post(
        f"/api/v1/integration/artifact-exports/{export.id}/acknowledge",
        {
            "manifest_digest": export.manifest_digest,
            "external_receipt": "mes-receipt-1",
        },
        format="json",
    )
    repeated = client.post(
        f"/api/v1/integration/artifact-exports/{export.id}/acknowledge",
        {
            "manifest_digest": export.manifest_digest,
            "external_receipt": "mes-receipt-1",
        },
        format="json",
    )
    assert acknowledged.status_code == 200
    assert repeated.status_code == 200
    assert acknowledged.data["acknowledged_at"]
    _validate_openapi_schema("ArtifactExport", acknowledged.data)
    listed = client.get(
        f"/api/v1/integration/analysis-runs/{run.id}/artifact-exports"
    )
    detailed = client.get(
        f"/api/v1/integration/artifact-exports/{export.id}"
    )
    assert listed.status_code == 200
    assert listed.data["view"] == "summary"
    assert listed.data["results"][0]["manifest"] is None
    assert detailed.status_code == 200
    assert detailed.data["manifest"]["export_id"] == str(export.id)
    _validate_openapi_schema("ArtifactExportList", listed.data)


@pytest.mark.django_db
def test_partial_file_failure_never_marks_export_or_run_delivered(
    artifact_workspace,
    monkeypatch,
):
    runs, destinations, profiles = artifact_workspace
    account, _ = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs, file_values=(b"first\n", b"second\n"))
    export, _ = create_artifact_export(
        run=run,
        account=account,
        idempotency_key="partial-export",
        profile_name="customer-results",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )
    original = artifact_exports_module._write_managed_stream
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ArtifactExportError(
                "ARTIFACT_EXPORT_DESTINATION_UNAVAILABLE",
                "temporary destination failure",
                retryable=True,
                http_status=503,
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        artifact_exports_module,
        "_write_managed_stream",
        fail_second,
    )
    claimed = claim_next_artifact_export()
    assert claimed is not None
    assert deliver_artifact_export(claimed) is True
    export.refresh_from_db()
    run.refresh_from_db()
    assert export.state == ArtifactExport.State.PENDING
    assert export.manifest == {}
    assert export.completed_at is None
    assert run.status == AnalysisRun.Status.SUCCEEDED
    assert not IntegrationOutboxEvent.objects.filter(
        event_type=ARTIFACT_EXPORT_EVENT_TYPE
    ).exists()
    assert list(destinations.rglob("*.tsv"))
    assert export.attempts.get(attempt_number=1).outcome == (
        ArtifactExportAttempt.Outcome.RETRY
    )
    failed_attempt = export.attempts.get(attempt_number=1)
    assert failed_attempt.files_total == 2
    assert failed_attempt.files_exported == 1
    assert failed_attempt.bytes_exported == len(b"first\n")

    monkeypatch.setattr(
        artifact_exports_module,
        "_write_managed_stream",
        original,
    )
    export.next_attempt_at = timezone.now()
    export.save(update_fields=["next_attempt_at", "updated_at"])
    retried = claim_next_artifact_export()
    assert retried is not None
    assert retried.id == export.id
    assert deliver_artifact_export(retried) is True
    export.refresh_from_db()
    assert export.state == ArtifactExport.State.SUCCEEDED
    assert export.attempts.get(attempt_number=2).outcome == (
        ArtifactExportAttempt.Outcome.SUCCEEDED
    )


class _FakeS3Destination:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.closed = False

    def head_object(self, **parameters):
        key = parameters["Key"]
        if key not in self.objects:
            raise ClientError(
                {
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                    "Error": {"Code": "404", "Message": "not found"},
                },
                "HeadObject",
            )
        item = self.objects[key]
        return {
            "ContentLength": len(item["body"]),
            "Metadata": item["metadata"],
            "ETag": f'"{item["etag"]}"',
            "VersionId": "version-1",
        }

    def put_object(self, **parameters):
        body = bytes(parameters["Body"])
        self.objects[parameters["Key"]] = {
            "body": body,
            "metadata": parameters["Metadata"],
            "etag": hashlib.md5(body, usedforsecurity=False).hexdigest(),
        }
        return {"ETag": self.objects[parameters["Key"]]["etag"]}

    def close(self):
        self.closed = True


@pytest.mark.django_db
def test_s3_export_verifies_sha_and_never_persists_credentials(
    artifact_workspace,
    monkeypatch,
):
    runs, _, profiles = artifact_workspace
    account, _ = _account()
    access_key, secret_key = _write_s3_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs, include_value=False)
    export, _ = create_artifact_export(
        run=run,
        account=account,
        idempotency_key="s3-export",
        profile_name="customer-s3",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )
    destination = _FakeS3Destination()
    monkeypatch.setattr(
        artifact_exports_module,
        "_s3_client",
        lambda _profile: destination,
    )

    claimed = claim_next_artifact_export()
    assert claimed is not None
    assert deliver_artifact_export(claimed) is True
    export.refresh_from_db()
    assert export.state == ArtifactExport.State.SUCCEEDED
    assert destination.closed is True
    item = export.manifest["items"][0]
    assert item["destination"]["type"] == "s3_object"
    assert item["destination"]["version_id"] == "version-1"
    assert item["destination"]["sha256"] == item["sha256"]
    persisted = json.dumps(
        {
            "target": export.target_snapshot,
            "manifest": export.manifest,
            "location": export.manifest_location,
            "event": IntegrationOutboxEvent.objects.get(
                event_type=ARTIFACT_EXPORT_EVENT_TYPE
            ).payload,
        }
    )
    assert access_key not in persisted
    assert secret_key not in persisted


@pytest.mark.django_db
def test_cleanup_is_dry_run_by_default_and_requires_ack_and_retention(
    artifact_workspace,
):
    runs, _, profiles = artifact_workspace
    account, client = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs)
    response = _post_export(client, run)
    export = ArtifactExport.objects.get(pk=response.data["id"])
    claimed = claim_next_artifact_export()
    assert claimed is not None
    assert deliver_artifact_export(claimed) is True
    export.refresh_from_db()
    export.retention.retain_until = timezone.now() - timedelta(seconds=1)
    export.retention.save(update_fields=["retain_until", "updated_at"])

    candidates = output_cleanup_candidates(run_id=run.id)
    assert candidates[0]["eligible"] is False
    assert "delivery_unacknowledged" in candidates[0]["blockers"]
    assert claim_next_output_cleanup(run_id=run.id) is None

    output = StringIO()
    call_command(
        "cleanup_analysis_outputs",
        "--run-id",
        str(run.id),
        stdout=output,
    )
    assert "DRY_RUN no files were deleted" in output.getvalue()
    assert Path(run.work_directory).is_dir()
    with pytest.raises(CommandError, match="--all-eligible"):
        call_command("cleanup_analysis_outputs", "--apply")

    acknowledge_artifact_export(
        export,
        manifest_digest=export.manifest_digest,
        external_receipt="mes-cleanup-receipt",
        actor="pytest",
    )
    retention = claim_next_output_cleanup(run_id=run.id)
    assert retention is not None
    finalized, released, error = clean_analysis_output(retention, actor="pytest")
    assert finalized is True
    assert error is None
    assert released > 0
    assert not Path(run.work_directory).exists()
    retention.refresh_from_db()
    run.refresh_from_db()
    assert retention.state == retention.State.CLEANED
    assert retention.quarantined_at is not None
    assert run.status == AnalysisRun.Status.SUCCEEDED
    assert run.output_status == AnalysisRun.OutputStatus.UNAVAILABLE
    assert run.output_manifest["integrity_version"] == 2

    listed = client.get(f"/api/v1/integration/analysis-runs/{run.id}/outputs")
    assert listed.status_code == 200
    assert "download_url" not in listed.data["results"][0]
    download = client.get(
        f"/api/v1/integration/analysis-runs/{run.id}/outputs/download",
        {"key": "ExportTask.file1"},
    )
    assert download.status_code == 410
    assert download.data["error"]["code"] == "ANALYSIS_OUTPUT_CLEANED"


@pytest.mark.django_db
def test_changed_profile_route_fails_closed_without_touching_analysis_status(
    artifact_workspace,
):
    runs, _, profiles = artifact_workspace
    account, _ = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs)
    export, _ = create_artifact_export(
        run=run,
        account=account,
        idempotency_key="route-change",
        profile_name="customer-results",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )
    (profiles / "customer-results.json").write_text(
        json.dumps(
            {
                "type": "managed_directory",
                "allowed_clients": [account.client_id],
                "directory": "customers/other",
                "root_alias": "mes-results",
            }
        ),
        encoding="utf-8",
    )
    claimed = claim_next_artifact_export()
    assert claimed is not None
    assert deliver_artifact_export(claimed) is True
    export.refresh_from_db()
    run.refresh_from_db()
    assert export.state == ArtifactExport.State.DEAD_LETTER
    assert export.last_error_code == "ARTIFACT_EXPORT_TARGET_CHANGED"
    assert run.status == AnalysisRun.Status.SUCCEEDED
    assert artifact_export_payload(export)["manifest"] is None

    replayed = artifact_exports_module.replay_artifact_export(
        export.id,
        actor="pytest",
    )
    assert replayed.state == ArtifactExport.State.PENDING
    assert replayed.replay_count == 1
    with pytest.raises(ArtifactExportError) as caught:
        artifact_exports_module.replay_artifact_export(export.id, actor="pytest")
    assert caught.value.code == "ARTIFACT_EXPORT_NOT_DEAD_LETTER"


@pytest.mark.django_db
def test_deactivated_service_account_cannot_finish_export(artifact_workspace):
    runs, destinations, profiles = artifact_workspace
    account, _ = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs)
    export, _ = create_artifact_export(
        run=run,
        account=account,
        idempotency_key="deactivated-account",
        profile_name="customer-results",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )
    claimed = claim_next_artifact_export()
    assert claimed is not None
    account.is_active = False
    account.save(update_fields=["is_active", "updated_at"])

    assert deliver_artifact_export(claimed) is True

    export.refresh_from_db()
    assert export.state == ArtifactExport.State.DEAD_LETTER
    assert export.last_error_code == "ARTIFACT_EXPORT_SERVICE_ACCOUNT_INACTIVE"
    assert list(destinations.rglob("manifest.json")) == []


@pytest.mark.django_db
def test_manifest_is_identical_after_crash_between_publish_and_commit(
    artifact_workspace,
):
    runs, destinations, profiles = artifact_workspace
    account, _ = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs)
    export, _ = create_artifact_export(
        run=run,
        account=account,
        idempotency_key="manifest-crash-recovery",
        profile_name="customer-results",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )
    first = claim_next_artifact_export()
    assert first is not None

    first_result = artifact_exports_module._perform_artifact_export(first)
    first_manifest, first_digest, first_location, *_ = first_result
    first_manifest_bytes = (
        destinations / first_location["relative_path"]
    ).read_bytes()
    first.refresh_from_db()
    assert first.state == ArtifactExport.State.EXPORTING
    assert first.manifest == first_manifest
    assert first.manifest_digest == first_digest
    ArtifactExport.objects.filter(pk=first.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    recovered = claim_next_artifact_export()
    assert recovered is not None
    assert recovered.id == export.id
    assert deliver_artifact_export(recovered) is True

    export.refresh_from_db()
    assert export.state == ArtifactExport.State.SUCCEEDED
    assert export.manifest == first_manifest
    assert export.manifest_digest == first_digest
    assert (destinations / export.manifest_location["relative_path"]).read_bytes() == (
        first_manifest_bytes
    )
    assert (
        IntegrationOutboxEvent.objects.filter(
            event_type=ARTIFACT_EXPORT_EVENT_TYPE,
            deduplication_key=f"export:{export.id}",
        ).count()
        == 1
    )


def test_s3_part_size_respects_the_ten_thousand_part_limit(settings):
    settings.ANALYSIS_ARTIFACT_EXPORT_CHUNK_BYTES = 8 * 1024 * 1024
    expected_size = 1024 * 1024 * 1024 * 1024

    part_size = artifact_exports_module._s3_part_size(expected_size)

    assert (expected_size + part_size - 1) // part_size <= 10_000
    assert part_size >= 5 * 1024 * 1024


def test_small_s3_upload_never_publishes_unverified_source(settings):
    settings.ANALYSIS_ARTIFACT_EXPORT_CHUNK_BYTES = 64 * 1024
    destination = _FakeS3Destination()
    profile = artifact_exports_module.ArtifactExportProfile(
        name="test-s3",
        kind="s3",
        allowed_clients=("mes",),
        endpoint_url="https://objects.example.test",
        region="us-east-1",
        bucket="deliveries",
    )
    expected = b"safe"

    with pytest.raises(ArtifactExportError) as caught:
        artifact_exports_module._write_s3_stream(
            destination,
            profile,
            object(),
            key="run/export/result.tsv",
            source=BytesIO(b"evil"),
            expected_size=len(expected),
            expected_sha256="sha256:" + hashlib.sha256(expected).hexdigest(),
            content_type="text/tab-separated-values",
        )

    assert caught.value.code == "ARTIFACT_EXPORT_SOURCE_CHANGED"
    assert destination.objects == {}


def test_s3_dns_failure_remains_retryable(monkeypatch):
    profile = artifact_exports_module.ArtifactExportProfile(
        name="test-s3",
        kind="s3",
        allowed_clients=("mes",),
        endpoint_url="https://objects.example.test",
        region="us-east-1",
        bucket="deliveries",
        access_key_id="access",
        secret_access_key="secret",
    )
    monkeypatch.setattr(
        artifact_exports_module,
        "_validate_endpoint",
        lambda _profile: (_ for _ in ()).throw(
            ObjectInputError(
                "OBJECT_INPUT_ENDPOINT_UNAVAILABLE",
                "dns unavailable",
                retryable=True,
                http_status=503,
            )
        ),
    )

    with pytest.raises(ArtifactExportError) as caught:
        artifact_exports_module._s3_client(profile)

    assert caught.value.code == "ARTIFACT_EXPORT_ENDPOINT_UNAVAILABLE"
    assert caught.value.retryable is True


def test_s3_endpoint_path_is_part_of_the_immutable_route():
    common = {
        "name": "test-s3",
        "kind": "s3",
        "allowed_clients": ("mes",),
        "region": "us-east-1",
        "bucket": "deliveries",
    }
    first = artifact_exports_module.ArtifactExportProfile(
        **common,
        endpoint_url="https://objects.example.test/minio-a",
    )
    second = artifact_exports_module.ArtifactExportProfile(
        **common,
        endpoint_url="https://objects.example.test/minio-b",
    )

    assert artifact_exports_module.artifact_export_target_snapshot(first) != (
        artifact_exports_module.artifact_export_target_snapshot(second)
    )


@pytest.mark.django_db
def test_managed_export_rejects_symlinked_destination_ancestor(
    artifact_workspace,
):
    runs, destinations, profiles = artifact_workspace
    account, _ = _account()
    _write_managed_profile(profiles, account)
    outside = destinations.parent / "outside"
    outside.mkdir()
    (destinations / "customers").symlink_to(outside, target_is_directory=True)
    run, _ = _run_with_outputs(account, runs)
    export, _ = create_artifact_export(
        run=run,
        account=account,
        idempotency_key="unsafe-destination",
        profile_name="customer-results",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )

    claimed = claim_next_artifact_export()
    assert claimed is not None
    assert deliver_artifact_export(claimed) is True

    export.refresh_from_db()
    assert export.state == ArtifactExport.State.DEAD_LETTER
    assert export.last_error_code == "ARTIFACT_EXPORT_PATH_UNSAFE"
    assert list(outside.iterdir()) == []


@pytest.mark.django_db
def test_cleanup_preflight_refuses_special_nodes_before_quarantine(
    artifact_workspace,
):
    runs, _, profiles = artifact_workspace
    account, _ = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs)
    export, _ = create_artifact_export(
        run=run,
        account=account,
        idempotency_key="cleanup-special-node",
        profile_name="customer-results",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )
    claimed = claim_next_artifact_export()
    assert claimed is not None
    assert deliver_artifact_export(claimed) is True
    export.refresh_from_db()
    export.retention.retain_until = timezone.now() - timedelta(seconds=1)
    export.retention.save(update_fields=["retain_until", "updated_at"])
    fifo = Path(run.work_directory) / "unexpected.fifo"
    os.mkfifo(fifo)

    retention = claim_next_output_cleanup(run_id=run.id)
    assert retention is not None
    finalized, _, error = clean_analysis_output(retention, actor="pytest")

    assert finalized is True
    assert error is not None
    assert error.code == "ANALYSIS_OUTPUT_CLEANUP_PATH_UNSAFE"
    retention.refresh_from_db()
    assert retention.state == retention.State.FAILED
    assert retention.quarantined_at is None
    assert Path(run.work_directory).is_dir()
    fifo.unlink()


@pytest.mark.django_db
def test_missing_output_tree_is_never_misreported_as_cleaned(
    artifact_workspace,
):
    runs, _, profiles = artifact_workspace
    account, _ = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs)
    export, _ = create_artifact_export(
        run=run,
        account=account,
        idempotency_key="cleanup-missing-tree",
        profile_name="customer-results",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )
    claimed = claim_next_artifact_export()
    assert claimed is not None
    assert deliver_artifact_export(claimed) is True
    export.refresh_from_db()
    export.retention.retain_until = timezone.now() - timedelta(seconds=1)
    export.retention.save(update_fields=["retain_until", "updated_at"])
    shutil.rmtree(run.work_directory)

    for _ in range(2):
        retention = claim_next_output_cleanup(run_id=run.id)
        assert retention is not None
        finalized, _, error = clean_analysis_output(retention, actor="pytest")
        assert finalized is True
        assert error is not None
        assert error.code == "ANALYSIS_OUTPUT_CLEANUP_SOURCE_MISSING"

    retention.refresh_from_db()
    run.refresh_from_db()
    assert retention.state == retention.State.FAILED
    assert retention.quarantined_at is None
    assert retention.cleaned_at is None
    assert run.output_status == AnalysisRun.OutputStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_postgresql_artifact_export_claim_and_lease_fencing(
    artifact_workspace,
):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking is required for this concurrency test")
    runs, _, profiles = artifact_workspace
    account, _ = _account()
    _write_managed_profile(profiles, account)
    run, _ = _run_with_outputs(account, runs)
    create_artifact_export(
        run=run,
        account=account,
        idempotency_key="postgres-claim",
        profile_name="customer-results",
        requires_ack=False,
        retain_until=None,
        actor="pytest",
    )
    first_holds_lock = Event()
    release_first = Event()
    create_lock = Lock()
    first_create = True
    original_create = ArtifactExportAttempt.objects.create

    def block_first_attempt_create(*args, **kwargs):
        nonlocal first_create
        with create_lock:
            should_block = first_create
            first_create = False
        if should_block:
            first_holds_lock.set()
            assert release_first.wait(5), "timed out waiting to release first claimant"
        return original_create(*args, **kwargs)

    def claim_on_independent_connection():
        close_old_connections()
        try:
            export = claim_next_artifact_export()
            if export is None:
                return None
            return export.id, export.lease_token
        finally:
            close_old_connections()

    with (
        patch.object(
            ArtifactExportAttempt.objects,
            "create",
            side_effect=block_first_attempt_create,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first_future = executor.submit(claim_on_independent_connection)
        assert first_holds_lock.wait(5), "first claimant did not acquire the row lock"
        second_future = executor.submit(claim_on_independent_connection)
        try:
            second_result = second_future.result(timeout=5)
        finally:
            release_first.set()
        first_result = first_future.result(timeout=5)

    assert first_result is not None
    assert second_result is None
    export_id, old_lease_token = first_result
    stale_export = ArtifactExport.objects.select_related("run", "retention").get(
        pk=export_id
    )
    ArtifactExport.objects.filter(pk=export_id).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    recovered = claim_next_artifact_export()

    assert recovered is not None
    assert recovered.id == export_id
    assert recovered.lease_token != old_lease_token
    assert (
        artifact_exports_module._finish_artifact_export(
            stale_export,
            result=None,
            error=ArtifactExportError(
                "ARTIFACT_EXPORT_DESTINATION_UNAVAILABLE",
                "stale worker",
                retryable=True,
            ),
        )
        is False
    )
    recovered.refresh_from_db()
    assert recovered.state == ArtifactExport.State.EXPORTING
    assert recovered.lease_token != old_lease_token
    assert recovered.attempt_count == 2

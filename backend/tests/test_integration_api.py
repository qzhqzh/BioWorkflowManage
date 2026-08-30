from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import uuid
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from rest_framework.test import APIClient

from compiler_core import canonical_digest, compile_workflow
from manage_mcp import TOOLS, handle, tool_call
from workflows import integration_api as integration_api_module
from workflows.analysis_runtime import (
    _finalize_cancelled_run,
    _verify_run_resource_manifests,
    claim_next_run,
    process_analysis_run,
)
from workflows.analysis_products import (
    AnalysisProductError,
    publish_analysis_product_version,
)
from workflows.integration_api import IntegrationAPIError, _managed_resource
from workflows.integration_outputs import (
    ResourceSnapshotBudget,
    ResourceSnapshotBudgetError,
    _directory_manifest,
    _sha256,
    build_output_manifest,
)
from workflows.integration_tokens import issue_service_token
from workflows.models import (
    AnalysisProduct,
    AnalysisProductVersion,
    AnalysisRun,
    InputStagingCoordinator,
    ServiceAccount,
    ServiceToken,
    SoftwareAsset,
    ToolVersion,
    WorkflowDocument,
    WorkflowVersion,
)
from workflows.object_inputs import ObjectInputError, stage_run_object_inputs


pytestmark = pytest.mark.usefixtures("auth_disabled")


ALL_SCOPES = [
    "analysis:submit",
    "analysis:read",
    "analysis:cancel",
    "analysis:retry",
    "analysis:download",
    "workflow:read",
    "library:read",
    "task:test",
]


def _token_client(*, client_id="okb", scopes=None):
    account = ServiceAccount.objects.create(
        client_id=client_id,
        name=client_id.upper(),
        scopes=scopes or ALL_SCOPES,
    )
    token, raw_token = issue_service_token(account, name="test", actor="pytest")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
    return account, token, raw_token, client


def _write_fastq(path: Path, mate: int, *, read_id="read-001"):
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(f"@{read_id}/{mate}\nACGT\n+\n!!!!\n")


def _fastq_bytes(mate: int, *, read_id="read-001") -> bytes:
    buffer = BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(f"@{read_id}/{mate}\nACGT\n+\n!!!!\n".encode())
    return buffer.getvalue()


def _write_object_profile(
    profile_dir: Path,
    *,
    name="lab-minio",
    allowed_client_ids=("okb",),
) -> tuple[str, str]:
    access_key = "test-access-key-not-for-storage"
    secret_key = "test-secret-key-not-for-storage"
    (profile_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "endpoint_url": "https://objects.example.test",
                "region": "us-east-1",
                "allowed_buckets": ["validated-inputs"],
                "allowed_client_ids": list(allowed_client_ids),
                "allowed_key_prefixes": {"validated-inputs": ["incoming/"]},
                "access_key_id": access_key,
                "secret_access_key": secret_key,
            }
        ),
        encoding="utf-8",
    )
    return access_key, secret_key


def _object_reference(content: bytes, *, key: str, version_id="version-1"):
    return {
        "type": "s3_object",
        "profile": "lab-minio",
        "bucket": "validated-inputs",
        "key": key,
        "version_id": version_id,
        "etag": "etag-" + hashlib.sha256(content).hexdigest()[:32],
        "size": len(content),
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
    }


class _FakeObjectBody(BytesIO):
    pass


class _FakeS3Client:
    def __init__(self, objects: dict[str, bytes], *, get_error=None):
        self.objects = objects
        self.get_error = get_error
        self.head_calls = []
        self.get_calls = []

    def _response(self, key: str):
        content = self.objects[key]
        return {
            "ContentLength": len(content),
            "ETag": f'"etag-{hashlib.sha256(content).hexdigest()[:32]}"',
            "VersionId": "version-1",
        }

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        return self._response(kwargs["Key"])

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        if self.get_error is not None:
            raise self.get_error
        return {
            **self._response(kwargs["Key"]),
            "Body": _FakeObjectBody(self.objects[kwargs["Key"]]),
        }

    def close(self):
        return None


@pytest.fixture
def integration_workspace(settings, tmp_path: Path):
    rawdata = tmp_path / "rawdata"
    database = tmp_path / "database"
    runs = tmp_path / "runs"
    staging = tmp_path / "input-staging"
    profiles = tmp_path / "object-storage-profiles"
    rawdata.mkdir()
    database.mkdir()
    runs.mkdir()
    staging.mkdir()
    profiles.mkdir()
    _write_fastq(rawdata / "S001_R1.fastq.gz", 1)
    _write_fastq(rawdata / "S001_R2.fastq.gz", 2)
    settings.ANALYSIS_RAWDATA_ROOT = rawdata
    settings.ANALYSIS_RAWDATA_EXECUTION_ROOT = rawdata
    settings.ANALYSIS_DATABASE_ROOT = database
    settings.ANALYSIS_DATABASE_EXECUTION_ROOT = database
    settings.ANALYSIS_RUN_ROOT = runs
    settings.ANALYSIS_RUN_EXECUTION_ROOT = runs
    settings.ANALYSIS_INPUT_STAGING_ROOT = staging
    settings.ANALYSIS_INPUT_STAGING_EXECUTION_ROOT = staging
    settings.ANALYSIS_OBJECT_STORAGE_PROFILE_DIR = profiles
    settings.ANALYSIS_OBJECT_STAGE_MIN_FREE_BYTES = 0
    settings.ANALYSIS_OBJECT_STAGE_SLOT_WAIT_SECONDS = 0
    settings.ANALYSIS_OBJECT_STAGE_TIMEOUT_SECONDS = 5
    settings.ANALYSIS_OBJECT_STAGE_LEASE_SECONDS = 60
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 0
    InputStagingCoordinator.objects.get_or_create(pk=1)
    return rawdata, database, runs


def _workflow_version() -> WorkflowVersion:
    tool = {
        "schema_version": "1.0.0",
        "id": "copy_read",
        "name": "copy_read",
        "display_name": "Copy read",
        "tool_version": "1.0.0",
        "description": "Small integration fixture",
        "container": {"engine": "docker", "image": "ubuntu:24.04"},
        "inputs": [
            {
                "name": "read1",
                "label": "Read 1",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r1",
                "required": True,
            },
            {
                "name": "read2",
                "label": "Read 2",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r2",
                "required": True,
            },
        ],
        "outputs": [
            {
                "name": "result",
                "label": "QC result",
                "wdl_type": "File",
                "semantic_type": "report.qc_tsv",
                "capture": {"mode": "path", "value": "result.tsv"},
            }
        ],
        "command": {
            "shell": "bash",
            "strict_mode": True,
            "template": 'cp "~{read1}" result.tsv\n',
        },
        "runtime": {"cpu": 1, "memory_gb": 1, "disk_gb": 1},
    }
    graph = {
        "schema_version": "1.0.0",
        "id": "integration_smoke",
        "name": "Integration smoke",
        "target": {
            "language": "wdl",
            "version": "1.0",
            "profile": "miniwdl-compatible",
        },
        "nodes": [
            {
                "id": "read1",
                "type": "workflow_input",
                "label": "Read 1",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "bio.fastq.gz.r1",
                    "required": True,
                },
            },
            {
                "id": "read2",
                "type": "workflow_input",
                "label": "Read 2",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "bio.fastq.gz.r2",
                    "required": True,
                },
            },
            {
                "id": "copy",
                "type": "tool",
                "label": "Copy",
                "tool_ref": {
                    "id": tool["id"],
                    "tool_version": tool["tool_version"],
                    "spec_version": tool["schema_version"],
                    "digest": canonical_digest(tool),
                },
                "parameter_values": {},
            },
            {
                "id": "result",
                "type": "workflow_output",
                "label": "QC result",
                "port": {
                    "name": "value",
                    "wdl_type": "File",
                    "semantic_type": "report.qc_tsv",
                    "required": True,
                },
            },
        ],
        "edges": [
            {"id": "e1", "source": {"node_id": "read1", "port": "value"}, "target": {"node_id": "copy", "port": "read1"}},
            {"id": "e2", "source": {"node_id": "read2", "port": "value"}, "target": {"node_id": "copy", "port": "read2"}},
            {"id": "e3", "source": {"node_id": "copy", "port": "result"}, "target": {"node_id": "result", "port": "value"}},
        ],
    }
    validation, artifacts = compile_workflow(graph, [tool])
    assert validation["status"] == "valid", validation
    bundle = {
        "entrypoint": "workflow.wdl",
        "files": {
            item["name"]: item["content"]
            for item in artifacts
            if item.get("media_type") == "application/wdl"
        },
        "call_count": 1,
    }
    document = WorkflowDocument.objects.create(
        slug="integration-smoke",
        name="Integration smoke",
        workflow_graph=graph,
        tool_specs=[tool],
    )
    return WorkflowVersion.objects.create(
        workflow=document,
        version=1,
        name=document.name,
        semantic_digest=canonical_digest(graph),
        workflow_graph=graph,
        tool_specs=[tool],
        compiled_bundle=bundle,
        compiled_digest=canonical_digest(bundle),
        compiler_profile="compiler-core-v1",
        interface_contract={
            "contract_version": "1.0.0",
            "inputs": [
                {**tool["inputs"][0], "name": "read1"},
                {**tool["inputs"][1], "name": "read2"},
            ],
            "outputs": [
                {
                    "name": "result",
                    "label": "QC result",
                    "wdl_type": "File",
                    "semantic_type": "report.qc_tsv",
                    "required": True,
                }
            ],
        },
    )


def _analysis_product_version(
    workflow_version: WorkflowVersion,
) -> AnalysisProductVersion:
    product = AnalysisProduct.objects.create(
        code="dna-panel",
        name="DNA Panel",
        description="Stable external analysis contract",
        created_by="pytest",
    )
    item, created = publish_analysis_product_version(
        product,
        contract_version="1.0.0",
        workflow_version=workflow_version,
        actor="pytest",
    )
    assert created is True
    return item


def _submission(version: WorkflowVersion, *, external_run_id="okb-run-1"):
    return {
        "external_ref": {
            "client_id": "okb",
            "external_run_id": external_run_id,
            "external_analysis_id": "analysis-1",
        },
        "workflow": {
            "source_type": "workflow_version",
            "version_id": version.pk,
            "expected_source_digest": version.compiled_digest,
        },
        "subject": {"sample_id": "S001"},
        "inputs": {
            "read1": {"root_alias": "rawdata", "relative_path": "S001_R1.fastq.gz"},
            "read2": {"root_alias": "rawdata", "relative_path": "S001_R2.fastq.gz"},
        },
        "metadata": {"product_code": "PANEL001"},
    }


@pytest.mark.django_db
def test_manage_analysis_product_publishes_immutable_catalog_contract():
    _, _, _, client = _token_client(scopes=["workflow:read"])
    version = _workflow_version()
    output = StringIO()

    call_command(
        "manage_analysis_product",
        code="dna-panel",
        name="DNA Panel",
        description="Stable contract",
        contract_version="1.0.0",
        workflow_version_id=version.pk,
        actor="pytest",
        stdout=output,
    )

    item = AnalysisProductVersion.objects.select_related("product").get()
    assert item.product.code == "dna-panel"
    assert item.workflow_version == version
    assert item.source_digest == version.compiled_digest
    assert item.interface_contract == version.interface_contract
    assert "PUBLISHED dna-panel@1.0.0" in output.getvalue()

    reused_output = StringIO()
    call_command(
        "manage_analysis_product",
        code="dna-panel",
        contract_version="1.0.0",
        workflow_version_id=version.pk,
        actor="pytest",
        stdout=reused_output,
    )
    assert AnalysisProductVersion.objects.count() == 1
    assert "REUSED dna-panel@1.0.0" in reused_output.getvalue()

    catalog = client.get("/api/v1/integration/analysis-products")
    assert catalog.status_code == 200
    assert len(catalog.data["results"]) == 1
    assert catalog.data["results"][0]["analysis_code"] == "dna-panel"
    assert catalog.data["results"][0]["contract_version"] == "1.0.0"
    assert catalog.data["results"][0]["ready"] is True
    detail = client.get(
        "/api/v1/integration/analysis-products/dna-panel/versions/1.0.0"
    )
    assert detail.status_code == 200
    assert detail.data["contract_digest"] == item.contract_digest
    assert detail.data["workflow"]["version_id"] == version.pk

    product = item.product
    with pytest.raises(ValidationError, match="code is immutable"):
        AnalysisProduct.objects.filter(pk=product.pk).update(code="renamed-product")
    product.code = "renamed-product"
    with pytest.raises(ValidationError, match="code is immutable"):
        product.save()

    with pytest.raises(ValidationError, match="cannot be updated"):
        AnalysisProductVersion.objects.filter(pk=item.pk).update(
            source_digest="sha256:" + "0" * 64
        )
    with pytest.raises(ValidationError, match="cannot be updated"):
        AnalysisProductVersion.objects.bulk_update(
            [item],
            ["contract_version"],
        )
    with pytest.raises(ValidationError, match="cannot be deleted"):
        AnalysisProductVersion.objects.filter(pk=item.pk).delete()
    item.contract_version = "2.0.0"
    with pytest.raises(ValidationError, match="snapshots are immutable"):
        item.save()

    second = WorkflowVersion.objects.create(
        workflow=version.workflow,
        version=2,
        name=version.name,
        description=version.description,
        kind=version.kind,
        semantic_digest=version.semantic_digest,
        workflow_graph=version.workflow_graph,
        editor_document=version.editor_document,
        tool_specs=version.tool_specs,
        compiled_bundle=version.compiled_bundle,
        compiled_digest=version.compiled_digest,
        compiler_profile=version.compiler_profile,
        interface_contract=version.interface_contract,
        subworkflow_references=version.subworkflow_references,
    )
    with pytest.raises(CommandError, match="ANALYSIS_PRODUCT_VERSION_CONFLICT"):
        call_command(
            "manage_analysis_product",
            code="dna-panel",
            contract_version="1.0.0",
            workflow_version_id=second.pk,
            actor="pytest",
        )
    with pytest.raises(CommandError, match="未找到可发布"):
        call_command(
            "manage_analysis_product",
            code="orphan-product",
            contract_version="1.0.0",
            workflow_version_id=999999,
            actor="pytest",
        )
    assert not AnalysisProduct.objects.filter(code="orphan-product").exists()


@pytest.mark.django_db
def test_analysis_product_publish_rejects_malformed_or_mismatched_contracts():
    version = _workflow_version()
    product = AnalysisProduct.objects.create(code="dna-panel", name="DNA Panel")
    valid = version.interface_contract
    malformed_input = {**valid, "inputs": [None]}
    duplicate_inputs = {
        **valid,
        "inputs": [
            valid["inputs"][0],
            {**valid["inputs"][1], "name": valid["inputs"][0]["name"]},
        ],
    }
    empty_inputs = {**valid, "inputs": []}
    missing_output_name = {
        **valid,
        "outputs": [{key: value for key, value in valid["outputs"][0].items() if key != "name"}],
    }

    for contract in (
        malformed_input,
        duplicate_inputs,
        empty_inputs,
        missing_output_name,
    ):
        WorkflowVersion.objects.filter(pk=version.pk).update(
            interface_contract=contract
        )
        version.refresh_from_db()
        with pytest.raises(AnalysisProductError) as caught:
            publish_analysis_product_version(
                product,
                contract_version="1.0.0",
                workflow_version=version,
                actor="pytest",
            )
        assert caught.value.code == "ANALYSIS_PRODUCT_CONTRACT_INVALID"
        assert not AnalysisProductVersion.objects.exists()


@pytest.mark.django_db
def test_analysis_product_preflight_and_submission_pin_contract(
    integration_workspace,
):
    account, _, _, client = _token_client()
    version = _workflow_version()
    product_version = _analysis_product_version(version)
    body = _submission(version)
    del body["workflow"]
    body["analysis_product"] = {
        "analysis_code": "dna-panel",
        "contract_version": "1.0.0",
    }

    preflight = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        body,
        format="json",
    )
    assert preflight.status_code == 200, preflight.data
    assert preflight.data["analysis_product"]["contract_digest"] == (
        product_version.contract_digest
    )
    assert preflight.data["workflow"]["id"] == version.pk

    created = client.post(
        "/api/v1/integration/analysis-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="product-run-1",
    )
    assert created.status_code == 201, created.data
    assert created.data["analysis_product"] == {
        "analysis_code": "dna-panel",
        "contract_version": "1.0.0",
        "contract_digest": product_version.contract_digest,
    }
    run = AnalysisRun.objects.get(pk=created.data["id"])
    assert run.service_account == account
    assert run.analysis_product_version == product_version
    assert run.workflow_version == version
    assert run.source_digest == version.compiled_digest
    assert run.request_payload["analysis_product"] == {
        "analysis_code": "dna-panel",
        "contract_version": "1.0.0",
        "contract_digest": product_version.contract_digest,
        "workflow_version_id": version.pk,
        "source_digest": version.compiled_digest,
    }

    run.status = AnalysisRun.Status.FAILED
    run.save(update_fields=["status", "updated_at"])
    retried = client.post(
        f"/api/v1/integration/analysis-runs/{run.id}/retry",
        {
            "external_ref": {
                "client_id": "okb",
                "external_run_id": "product-run-retry",
            }
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="product-run-retry",
    )
    assert retried.status_code == 201, retried.data
    retry = AnalysisRun.objects.get(pk=retried.data["id"])
    assert retry.analysis_product_version == product_version
    assert retried.data["analysis_product"]["contract_digest"] == (
        product_version.contract_digest
    )

    conflict = _submission(version, external_run_id="product-conflict")
    conflict["analysis_product"] = body["analysis_product"]
    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        conflict,
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_SOURCE_CONFLICT"

    product_version.product.is_active = False
    product_version.product.save(update_fields=["is_active", "updated_at"])
    blocked_retry = client.post(
        f"/api/v1/integration/analysis-runs/{run.id}/retry",
        {
            "external_ref": {
                "client_id": "okb",
                "external_run_id": "product-run-retry-inactive",
            }
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="product-run-retry-inactive",
    )
    assert blocked_retry.status_code == 409
    assert blocked_retry.data["error"]["code"] == "ANALYSIS_PRODUCT_INACTIVE"
    conflict["workflow"] = None
    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        conflict,
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "ANALYSIS_SOURCE_CONFLICT"


@pytest.mark.django_db
def test_analysis_product_rejects_inactive_or_changed_snapshot(
    integration_workspace,
):
    _, _, _, client = _token_client()
    version = _workflow_version()
    product_version = _analysis_product_version(version)
    body = _submission(version)
    body.pop("workflow")
    body["analysis_product"] = {
        "analysis_code": "dna-panel",
        "contract_version": "1.0.0",
    }

    product_version.product.is_active = False
    product_version.product.save(update_fields=["is_active", "updated_at"])
    inactive = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        body,
        format="json",
    )
    assert inactive.status_code == 409
    assert inactive.data["error"]["code"] == "ANALYSIS_PRODUCT_INACTIVE"
    assert client.get("/api/v1/integration/analysis-products").data["results"] == []

    product_version.product.is_active = True
    product_version.product.save(update_fields=["is_active", "updated_at"])
    changed_contract = {
        **version.interface_contract,
        "outputs": [
            {
                **version.interface_contract["outputs"][0],
                "semantic_type": "report.changed",
            }
        ],
    }
    WorkflowVersion.objects.filter(pk=version.pk).update(
        interface_contract=changed_contract
    )
    changed = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        body,
        format="json",
    )
    assert changed.status_code == 409
    assert changed.data["error"]["code"] == "ANALYSIS_PRODUCT_SNAPSHOT_CHANGED"


@pytest.mark.django_db
def test_analysis_product_deactivation_during_submission_blocks_run(
    integration_workspace,
):
    _, _, _, client = _token_client()
    version = _workflow_version()
    product_version = _analysis_product_version(version)
    body = _submission(version, external_run_id="product-race")
    body.pop("workflow")
    body["analysis_product"] = {
        "analysis_code": "dna-panel",
        "contract_version": "1.0.0",
    }
    real_preflight = integration_api_module._preflight_workflow

    def deactivate_after_preflight(request_body, *, client_id):
        result = real_preflight(request_body, client_id=client_id)
        AnalysisProduct.objects.filter(pk=product_version.product_id).update(
            is_active=False
        )
        return result

    with patch.object(
        integration_api_module,
        "_preflight_workflow",
        side_effect=deactivate_after_preflight,
    ):
        response = client.post(
            "/api/v1/integration/analysis-runs",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY="product-race",
        )

    assert response.status_code == 409
    assert response.data["error"]["code"] == "ANALYSIS_PRODUCT_INACTIVE"
    assert not AnalysisRun.objects.filter(external_run_id="product-race").exists()


@pytest.mark.django_db
def test_analysis_product_deactivation_during_retry_blocks_run(
    integration_workspace,
):
    _, _, _, client = _token_client()
    version = _workflow_version()
    product_version = _analysis_product_version(version)
    body = _submission(version, external_run_id="product-retry-source")
    body.pop("workflow")
    body["analysis_product"] = {
        "analysis_code": "dna-panel",
        "contract_version": "1.0.0",
    }
    created = client.post(
        "/api/v1/integration/analysis-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="product-retry-source",
    )
    assert created.status_code == 201, created.data
    original = AnalysisRun.objects.get(pk=created.data["id"])
    original.status = AnalysisRun.Status.FAILED
    original.save(update_fields=["status", "updated_at"])

    def deactivate_during_manifest_check(*args, **kwargs):
        AnalysisProduct.objects.filter(pk=product_version.product_id).update(
            is_active=False
        )

    with patch.object(
        integration_api_module,
        "_verify_run_resource_manifests",
        side_effect=deactivate_during_manifest_check,
    ):
        response = client.post(
            f"/api/v1/integration/analysis-runs/{original.id}/retry",
            {
                "external_ref": {
                    "client_id": "okb",
                    "external_run_id": "product-retry-race",
                }
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="product-retry-race",
        )

    assert response.status_code == 409
    assert response.data["error"]["code"] == "ANALYSIS_PRODUCT_INACTIVE"
    assert not AnalysisRun.objects.filter(
        external_run_id="product-retry-race"
    ).exists()


@pytest.mark.django_db
def test_analysis_product_missing_uses_stable_error_contract():
    _, _, _, client = _token_client()
    version = _workflow_version()
    missing_detail = client.get(
        "/api/v1/integration/analysis-products/missing/versions/1.0.0",
        HTTP_X_REQUEST_ID="missing-product-detail",
    )
    assert missing_detail.status_code == 404
    assert missing_detail.data["error"]["code"] == (
        "ANALYSIS_PRODUCT_VERSION_NOT_FOUND"
    )
    assert missing_detail.data["error"]["request_id"] == "missing-product-detail"
    assert missing_detail["X-Request-ID"] == "missing-product-detail"

    body = _submission(version, external_run_id="missing-product-run")
    body.pop("workflow")
    body["analysis_product"] = {
        "analysis_code": "missing",
        "contract_version": "1.0.0",
    }
    preflight = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        body,
        format="json",
    )
    submitted = client.post(
        "/api/v1/integration/analysis-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="missing-product-run",
    )
    for response in (preflight, submitted):
        assert response.status_code == 404
        assert response.data["error"]["code"] == (
            "ANALYSIS_PRODUCT_VERSION_NOT_FOUND"
        )


@pytest.mark.django_db
def test_service_token_auth_scope_revocation_and_isolation(settings):
    settings.AUTH_REQUIRED = True
    account, token, raw_token, client = _token_client(scopes=["workflow:read"])
    other, _, _, other_client = _token_client(client_id="other")
    version = _workflow_version()

    unauthenticated = APIClient().get(
        "/api/v1/integration/workflow-versions",
        HTTP_X_REQUEST_ID="okb-auth-check",
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.data["error"]["code"] == "SERVICE_AUTHENTICATION_REQUIRED"
    assert unauthenticated.data["error"]["request_id"] == "okb-auth-check"
    assert unauthenticated["X-Request-ID"] == "okb-auth-check"
    assert client.get("/api/v1/integration/workflow-versions").status_code == 200
    forbidden = client.post("/api/v1/integration/analysis-runs/preflight", {}, format="json")
    assert forbidden.status_code == 403
    assert forbidden.data["error"]["code"] == "SERVICE_SCOPE_REQUIRED"

    run = AnalysisRun.objects.create(
        workflow_version=version,
        service_account=account,
        external_run_id="hidden-run",
        idempotency_key="hidden-idempotency",
        workflow_name="integration_smoke",
        sample_id="S001",
        actor="service:okb",
        source_bundle=version.compiled_bundle,
        source_digest=version.compiled_digest,
    )
    assert other_client.get(f"/api/v1/integration/analysis-runs/{run.id}").status_code == 404

    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])
    revoked = client.get("/api/v1/integration/workflow-versions")
    assert revoked.status_code == 401
    assert revoked.data["error"]["retryable"] is False
    assert ServiceToken.objects.get(pk=token.pk).token_hash != raw_token
    assert other.client_id == "other"


@pytest.mark.django_db
def test_service_token_is_rejected_by_legacy_browser_api(settings):
    settings.AUTH_REQUIRED = True
    _, _, _, client = _token_client(scopes=["workflow:read"])

    response = client.get("/api/v1/analysis-runs")

    assert response.status_code == 403


@pytest.mark.django_db
def test_service_account_token_rotation_does_not_reactivate_disabled_account():
    account = ServiceAccount.objects.create(
        client_id="disabled",
        name="Disabled",
        scopes=["workflow:read"],
        is_active=False,
    )
    token, _ = issue_service_token(account, name="old", actor="pytest")

    output = StringIO()
    call_command(
        "manage_service_account",
        client_id="disabled",
        revoke_prefix=token.prefix,
        actor="pytest",
        stdout=output,
    )
    account.refresh_from_db()
    token.refresh_from_db()
    assert account.is_active is False
    assert token.revoked_at is not None

    call_command(
        "manage_service_account",
        client_id="disabled",
        activate=True,
        actor="pytest",
        stdout=StringIO(),
    )
    account.refresh_from_db()
    assert account.is_active is True


@pytest.mark.django_db
def test_integration_not_found_and_validation_errors_use_stable_envelope(settings):
    settings.AUTH_REQUIRED = True
    _, _, _, client = _token_client()
    missing = client.get(f"/api/v1/integration/analysis-runs/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.data["error"]["code"] == "INTEGRATION_RESOURCE_NOT_FOUND"
    assert missing.data["error"]["category"] == "validation"
    assert missing["X-Request-ID"] == missing.data["error"]["request_id"]

    invalid_batch = client.post(
        "/api/v1/integration/analysis-runs/batch-status",
        {"run_ids": ["not-a-uuid"]},
        format="json",
    )
    assert invalid_batch.status_code == 400
    assert invalid_batch.data["error"]["code"] == "BATCH_STATUS_RUN_ID_INVALID"


@pytest.mark.django_db
def test_s3_inputs_share_auditable_manifest_and_stage_without_credentials(
    integration_workspace,
    settings,
):
    _, _, _, client = _token_client()
    version = _workflow_version()
    read1 = _fastq_bytes(1)
    read2 = _fastq_bytes(2)
    objects = {
        "incoming/S001_R1.fastq.gz": read1,
        "incoming/S001_R2.fastq.gz": read2,
    }
    access_key, secret_key = _write_object_profile(
        Path(settings.ANALYSIS_OBJECT_STORAGE_PROFILE_DIR)
    )
    body = _submission(version, external_run_id="portable-input-run")
    body["inputs"] = {
        "read1": _object_reference(read1, key="incoming/S001_R1.fastq.gz"),
        "read2": _object_reference(read2, key="incoming/S001_R2.fastq.gz"),
    }
    fake_client = _FakeS3Client(objects)

    with (
        patch("workflows.object_inputs._validate_endpoint"),
        patch("workflows.object_inputs._s3_client", return_value=fake_client),
    ):
        preflight = client.post(
            "/api/v1/integration/analysis-runs/preflight",
            body,
            format="json",
        )
        created = client.post(
            "/api/v1/integration/analysis-runs",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY="portable-input-run",
        )

    assert preflight.status_code == 200, preflight.data
    assert preflight.data["ready"] is True
    assert [
        item["check"] for item in preflight.data["checks"]
    ].count("object_input_head") == 2
    manifest = preflight.data["resource_manifest"]["input_resource_manifest"]
    assert manifest["schema_version"] == 2
    assert manifest["files"] == []
    assert len(manifest["objects"]) == 2
    assert all(item["reference_type"] == "s3_object" for item in manifest["objects"])
    assert created.status_code == 201, created.data

    run = AnalysisRun.objects.get(pk=created.data["id"])
    stored = json.dumps(
        {"request": run.request_payload, "inputs": run.input_values},
        ensure_ascii=False,
    )
    public = json.dumps(
        {"preflight": preflight.data, "created": created.data},
        ensure_ascii=False,
        default=str,
    )
    for secret in (access_key, secret_key):
        assert secret not in stored
        assert secret not in public
    assert all(path.endswith(".fastq.gz") for path in run.input_values.values())

    run.status = AnalysisRun.Status.PREPARING
    run.lease_token = uuid.uuid4()
    run.save(update_fields=["status", "lease_token", "updated_at"])
    with (
        patch("workflows.object_inputs._validate_endpoint"),
        patch("workflows.object_inputs._s3_client", return_value=fake_client),
    ):
        staged_count = stage_run_object_inputs(run)

    assert staged_count == 2
    for item in run.request_payload["input_resource_manifest"]["objects"]:
        staged = Path(settings.ANALYSIS_INPUT_STAGING_ROOT) / item["staging_relative_path"]
        assert staged.read_bytes() == objects[item["key"]]
        assert staged.stat().st_mode & 0o222 == 0
    assert len(fake_client.get_calls) == 2
    assert all(call["VersionId"] == "version-1" for call in fake_client.get_calls)
    assert all(call["IfMatch"].startswith('"etag-') for call in fake_client.get_calls)
    assert all(call["IfMatch"].endswith('"') for call in fake_client.get_calls)

    first = run.request_payload["input_resource_manifest"]["objects"][0]
    staged = Path(settings.ANALYSIS_INPUT_STAGING_ROOT) / first["staging_relative_path"]
    staged.chmod(0o644)
    staged.write_bytes(b"x" * first["size"])
    staged.chmod(0o444)
    with pytest.raises(ObjectInputError) as caught:
        stage_run_object_inputs(run)
    assert caught.value.code == "OBJECT_INPUT_STAGING_CHANGED"


@pytest.mark.django_db
def test_s3_input_changes_and_conditional_get_fail_closed(
    integration_workspace,
    settings,
):
    _, _, _, client = _token_client()
    version = _workflow_version()
    read1 = _fastq_bytes(1)
    _write_object_profile(Path(settings.ANALYSIS_OBJECT_STORAGE_PROFILE_DIR))
    body = _submission(version, external_run_id="changed-object-run")
    body["inputs"]["read1"] = _object_reference(
        read1,
        key="incoming/S001_R1.fastq.gz",
    )
    changed_client = _FakeS3Client(
        {"incoming/S001_R1.fastq.gz": b"x" * len(read1)}
    )
    with (
        patch("workflows.object_inputs._validate_endpoint"),
        patch("workflows.object_inputs._s3_client", return_value=changed_client),
    ):
        changed = client.post(
            "/api/v1/integration/analysis-runs/preflight",
            body,
            format="json",
        )
    assert changed.status_code == 409
    assert changed.data["error"]["code"] == "OBJECT_INPUT_CHANGED"

    forbidden = json.loads(json.dumps(body))
    forbidden["inputs"]["read1"]["bucket"] = "unapproved-inputs"
    bucket_response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        forbidden,
        format="json",
    )
    assert bucket_response.status_code == 403
    assert bucket_response.data["error"]["code"] == "OBJECT_INPUT_BUCKET_FORBIDDEN"

    credential_injection = json.loads(json.dumps(body))
    credential_injection["inputs"]["read1"]["secret_access_key"] = "must-not-echo"
    rejected_credential = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        credential_injection,
        format="json",
    )
    assert rejected_credential.status_code == 400
    assert rejected_credential.data["error"]["code"] == "OBJECT_INPUT_REFERENCE_INVALID"
    assert "must-not-echo" not in json.dumps(rejected_credential.data)

    good_client = _FakeS3Client({"incoming/S001_R1.fastq.gz": read1})
    with (
        patch("workflows.object_inputs._validate_endpoint"),
        patch("workflows.object_inputs._s3_client", return_value=good_client),
    ):
        created = client.post(
            "/api/v1/integration/analysis-runs",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY="changed-object-run",
        )
    assert created.status_code == 201, created.data
    run = AnalysisRun.objects.get(pk=created.data["id"])
    run.status = AnalysisRun.Status.PREPARING
    run.lease_token = uuid.uuid4()
    run.save(update_fields=["status", "lease_token", "updated_at"])
    precondition_failed = ClientError(
        {
            "Error": {"Code": "PreconditionFailed", "Message": "changed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "GetObject",
    )
    failed_client = _FakeS3Client(
        {"incoming/S001_R1.fastq.gz": read1},
        get_error=precondition_failed,
    )
    with (
        patch("workflows.object_inputs._validate_endpoint"),
        patch("workflows.object_inputs._s3_client", return_value=failed_client),
        pytest.raises(ObjectInputError) as caught,
    ):
        stage_run_object_inputs(run)
    assert caught.value.code == "OBJECT_INPUT_CHANGED"

    manifest_item = run.request_payload["input_resource_manifest"]["objects"][0]
    cached = Path(settings.ANALYSIS_INPUT_STAGING_ROOT) / manifest_item[
        "staging_relative_path"
    ]
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(read1)
    cached.chmod(0o444)
    corrupt_client = _FakeS3Client({"incoming/S001_R1.fastq.gz": read1})

    def corrupt_get(**kwargs):
        corrupt_client.get_calls.append(kwargs)
        return {
            **corrupt_client._response(kwargs["Key"]),
            "Body": _FakeObjectBody(b"x" * len(read1)),
        }

    corrupt_client.get_object = corrupt_get
    with (
        patch("workflows.object_inputs._validate_endpoint"),
        patch("workflows.object_inputs._s3_client", return_value=corrupt_client),
        pytest.raises(ObjectInputError) as digest_error,
    ):
        stage_run_object_inputs(run)
    assert digest_error.value.code == "OBJECT_INPUT_DIGEST_MISMATCH"
    assert len(corrupt_client.get_calls) == 1


@pytest.mark.django_db
def test_s3_profile_enforces_service_account_and_key_prefix(
    integration_workspace,
    settings,
):
    version = _workflow_version()
    content = _fastq_bytes(1)
    _write_object_profile(Path(settings.ANALYSIS_OBJECT_STORAGE_PROFILE_DIR))

    _, _, _, other_client = _token_client(client_id="other")
    other_body = _submission(version, external_run_id="other-client-object")
    other_body["external_ref"]["client_id"] = "other"
    other_body["inputs"]["read1"] = _object_reference(
        content,
        key="incoming/S001_R1.fastq.gz",
    )
    forbidden_profile = other_client.post(
        "/api/v1/integration/analysis-runs/preflight",
        other_body,
        format="json",
    )
    assert forbidden_profile.status_code == 403
    assert forbidden_profile.data["error"]["code"] == "OBJECT_INPUT_PROFILE_FORBIDDEN"

    _, _, _, client = _token_client()
    key_body = _submission(version, external_run_id="outside-prefix-object")
    key_body["inputs"]["read1"] = _object_reference(
        content,
        key="private/S001_R1.fastq.gz",
    )
    forbidden_key = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        key_body,
        format="json",
    )
    assert forbidden_key.status_code == 403
    assert forbidden_key.data["error"]["code"] == "OBJECT_INPUT_KEY_FORBIDDEN"


@pytest.mark.django_db
def test_s3_reference_with_surrogate_key_returns_stable_validation_error(
    integration_workspace,
    settings,
):
    _, _, _, client = _token_client()
    version = _workflow_version()
    content = _fastq_bytes(1)
    _write_object_profile(Path(settings.ANALYSIS_OBJECT_STORAGE_PROFILE_DIR))
    body = _submission(version, external_run_id="surrogate-object-key")
    body["inputs"]["read1"] = _object_reference(
        content,
        key="incoming/" + chr(0xD800) + ".fastq.gz",
    )

    response = client.generic(
        "POST",
        "/api/v1/integration/analysis-runs/preflight",
        json.dumps(body).encode("ascii"),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "OBJECT_INPUT_REFERENCE_INVALID"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("invalid-gzip", "FASTQ_GZIP_INVALID"),
        ("mismatched-pair", "FASTQ_PAIR_MISMATCH"),
    ],
)
def test_s3_stage_validates_remote_fastq_content(
    integration_workspace,
    settings,
    case,
    expected_code,
):
    _, _, _, client = _token_client()
    version = _workflow_version()
    read1 = b"not-a-gzip-stream" if case == "invalid-gzip" else _fastq_bytes(1)
    read2 = _fastq_bytes(
        2,
        read_id="different-read" if case == "mismatched-pair" else "read-001",
    )
    objects = {
        "incoming/S001_R1.fastq.gz": read1,
        "incoming/S001_R2.fastq.gz": read2,
    }
    _write_object_profile(Path(settings.ANALYSIS_OBJECT_STORAGE_PROFILE_DIR))
    body = _submission(version, external_run_id=f"remote-fastq-{case}")
    body["inputs"] = {
        "read1": _object_reference(read1, key="incoming/S001_R1.fastq.gz"),
        "read2": _object_reference(read2, key="incoming/S001_R2.fastq.gz"),
    }
    fake_client = _FakeS3Client(objects)
    with (
        patch("workflows.object_inputs._validate_endpoint"),
        patch("workflows.object_inputs._s3_client", return_value=fake_client),
    ):
        created = client.post(
            "/api/v1/integration/analysis-runs",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"remote-fastq-{case}",
        )
    assert created.status_code == 201, created.data
    run = AnalysisRun.objects.get(pk=created.data["id"])
    run.status = AnalysisRun.Status.PREPARING
    run.lease_token = uuid.uuid4()
    run.save(update_fields=["status", "lease_token", "updated_at"])

    with (
        patch("workflows.object_inputs._validate_endpoint"),
        patch("workflows.object_inputs._s3_client", return_value=fake_client),
        pytest.raises(ObjectInputError) as caught,
    ):
        stage_run_object_inputs(run)

    assert caught.value.code == expected_code


@pytest.mark.django_db
def test_managed_input_keeps_legacy_custom_type_compatible(integration_workspace):
    _, _, _, client = _token_client()
    version = _workflow_version()
    body = _submission(version, external_run_id="legacy-managed-type")
    body["inputs"]["read1"]["type"] = "legacy_rawdata_file"

    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        body,
        format="json",
    )

    assert response.status_code == 200, response.data
    assert response.data["ready"] is True


@pytest.mark.django_db
def test_preflight_rejects_unsafe_paths_and_bad_fastq_pair(integration_workspace):
    _, _, _, client = _token_client()
    version = _workflow_version()
    body = _submission(version)

    ready = client.post("/api/v1/integration/analysis-runs/preflight", body, format="json")
    assert ready.status_code == 200, ready.data
    assert ready.data["ready"] is True
    assert {item["check"] for item in ready.data["checks"]} >= {
        "workflow_snapshot",
        "fastq_pair",
        "output_contract",
    }

    digest_mismatch = _submission(version)
    digest_mismatch["inputs"]["read1"]["sha256"] = "sha256:" + "0" * 64
    rejected = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        digest_mismatch,
        format="json",
    )
    assert rejected.status_code == 400
    assert rejected.data["error"]["code"] == "MANAGED_RESOURCE_DIGEST_MISMATCH"

    unsafe = _submission(version)
    unsafe["inputs"]["read1"]["relative_path"] = "../escape.fastq.gz"
    rejected = client.post("/api/v1/integration/analysis-runs/preflight", unsafe, format="json")
    assert rejected.status_code == 400
    assert rejected.data["error"]["code"] == "MANAGED_RESOURCE_PATH_INVALID"

    rawdata, _, _ = integration_workspace
    _write_fastq(rawdata / "S001_R2.fastq.gz", 2, read_id="different")
    mismatch = client.post("/api/v1/integration/analysis-runs/preflight", body, format="json")
    assert mismatch.status_code == 400
    assert mismatch.data["error"]["code"] == "FASTQ_PAIR_MISMATCH"

    nul_path = _submission(version)
    nul_path["inputs"]["read1"]["relative_path"] = "bad\x00.fastq.gz"
    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        nul_path,
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "MANAGED_RESOURCE_INVALID"

    rawdata, _, _ = integration_workspace
    (rawdata / "invalid_R1.fastq.gz").write_bytes(b"not a gzip stream")
    invalid_gzip = _submission(version)
    invalid_gzip["inputs"]["read1"]["relative_path"] = "invalid_R1.fastq.gz"
    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        invalid_gzip,
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "FASTQ_GZIP_INVALID"


@pytest.mark.django_db
def test_preflight_rejects_oversized_gzip_header(
    integration_workspace, settings
):
    rawdata, _, _ = integration_workspace
    _, _, _, client = _token_client()
    version = _workflow_version()
    settings.ANALYSIS_INPUT_GZIP_HEADER_MAX_BYTES = 32
    header_with_unterminated_name = b"\x1f\x8b\x08\x08" + b"\x00" * 6
    (rawdata / "S001_R1.fastq.gz").write_bytes(
        header_with_unterminated_name + b"x" * 4096
    )

    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        _submission(version),
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "FASTQ_GZIP_INVALID"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "invalid_gzip",
    [
        b"\x1f\x8b\x08\x00" + b"\x00" * 6,
        b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"not-deflate",
        gzip.compress(b"")
        + b"\x1f\x8b\x08\x08"
        + b"\x00" * 6
        + b"x" * 4096,
    ],
    ids=["truncated", "invalid-deflate", "record-in-second-member"],
)
def test_preflight_rejects_malformed_or_cross_member_gzip(
    integration_workspace, settings, invalid_gzip
):
    rawdata, _, _ = integration_workspace
    _, _, _, client = _token_client()
    version = _workflow_version()
    settings.ANALYSIS_INPUT_GZIP_HEADER_MAX_BYTES = 32
    (rawdata / "S001_R1.fastq.gz").write_bytes(invalid_gzip)

    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        _submission(version),
        format="json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "FASTQ_GZIP_INVALID"


@pytest.mark.django_db
def test_managed_directory_input_is_content_manifested_and_verified(
    settings, tmp_path
):
    database = tmp_path / "database"
    resource = database / "bundle"
    resource.mkdir(parents=True)
    child = resource / "reference.fa"
    child.write_bytes(b">chr1\nACGT\n")
    settings.ANALYSIS_DATABASE_ROOT = database
    settings.ANALYSIS_DATABASE_EXECUTION_ROOT = database
    manifests = {"database": []}
    observed = []

    _managed_resource(
        {"root_alias": "database", "relative_path": "bundle"},
        kind="directory",
        input_name="reference",
        semantic_type="bio.annotation.database_dir",
        manifests=manifests,
        observed=observed,
        snapshot_budget=ResourceSnapshotBudget(),
    )
    item = manifests["database"][0]
    assert item["verification"] == "directory_identity_sha256"
    assert item["digest"].startswith("sha256:")
    assert item["entry_count"] == 1

    run = type(
        "Run",
        (),
        {
            "request_payload": {
                "database_resource_manifest": {
                    "schema_version": 1,
                    "resources": manifests["database"],
                }
            }
        },
    )()
    child_stat = child.stat()
    time.sleep(0.01)
    child.write_bytes(b">chr1\nTGCA\n")
    os.utime(child, ns=(child_stat.st_atime_ns, child_stat.st_mtime_ns))

    with pytest.raises(RuntimeError, match="目录校验和不匹配"):
        _verify_run_resource_manifests(run)


@pytest.mark.django_db
def test_managed_file_explicit_sha_records_sha256_verification(settings, tmp_path):
    rawdata = tmp_path / "rawdata"
    resource = rawdata / "sample.fastq.gz"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"content")
    settings.ANALYSIS_RAWDATA_ROOT = rawdata
    settings.ANALYSIS_RAWDATA_EXECUTION_ROOT = rawdata
    manifests = {"rawdata": []}

    _managed_resource(
        {
            "root_alias": "rawdata",
            "relative_path": resource.name,
            "sha256": _sha256(resource),
        },
        kind="file",
        input_name="read1",
        semantic_type="bio.fastq.gz.r1",
        manifests=manifests,
        observed=[],
        snapshot_budget=ResourceSnapshotBudget(),
    )

    assert manifests["rawdata"][0]["verification"] == "sha256"
    assert manifests["rawdata"][0]["sha256"] == _sha256(resource)


@pytest.mark.django_db
def test_managed_directory_ignores_legacy_file_sha_field(settings, tmp_path):
    database = tmp_path / "database"
    resource = database / "bundle"
    resource.mkdir(parents=True)
    (resource / "reference.fa").write_text(">chr1\n", encoding="utf-8")
    settings.ANALYSIS_DATABASE_ROOT = database
    settings.ANALYSIS_DATABASE_EXECUTION_ROOT = database
    manifests = {"database": []}

    _managed_resource(
        {
            "root_alias": "database",
            "relative_path": "bundle",
            "sha256": "sha256:" + "0" * 64,
            "identity_digest": _directory_manifest(resource)["digest"],
        },
        kind="directory",
        input_name="reference",
        semantic_type="bio.annotation.database_dir",
        manifests=manifests,
        observed=[],
        snapshot_budget=ResourceSnapshotBudget(),
    )

    item = manifests["database"][0]
    assert item["warning"] == "legacy_directory_sha256_ignored"
    assert item["declared_identity_digest"] == _directory_manifest(resource)["digest"]


@pytest.mark.django_db
def test_managed_resource_snapshot_budget_caches_directories_and_limits_total(
    settings, tmp_path
):
    database = tmp_path / "database"
    resource = database / "bundle"
    resource.mkdir(parents=True)
    (resource / "reference.fa").write_bytes(b">chr1\nACGT\n")
    settings.ANALYSIS_DATABASE_ROOT = database
    settings.ANALYSIS_DATABASE_EXECUTION_ROOT = database
    manifests = {"database": []}
    observed = []
    budget = ResourceSnapshotBudget(max_resources=2)
    value = {"root_alias": "database", "relative_path": "bundle"}
    expected_manifest = _directory_manifest(resource)

    with patch(
        "workflows.integration_outputs._directory_manifest_isolated",
        return_value=expected_manifest,
    ) as snapshot:
        for input_name in ("reference", "annotation"):
            _managed_resource(
                value,
                kind="directory",
                input_name=input_name,
                semantic_type="bio.annotation.database_dir",
                manifests=manifests,
                observed=observed,
                snapshot_budget=budget,
            )
        assert snapshot.call_count == 1

    with pytest.raises(IntegrationAPIError) as captured:
        _managed_resource(
            value,
            kind="directory",
            input_name="third",
            semantic_type="bio.annotation.database_dir",
            manifests=manifests,
            observed=observed,
            snapshot_budget=budget,
        )
    assert captured.value.code == "MANAGED_RESOURCE_LIMIT_EXCEEDED"


@pytest.mark.django_db
def test_managed_file_input_records_identity_and_rejects_same_stat_replacement(
    integration_workspace,
):
    rawdata, _, _ = integration_workspace
    _, _, _, client = _token_client()
    version = _workflow_version()
    submitted = client.post(
        "/api/v1/integration/analysis-runs",
        _submission(version),
        format="json",
        HTTP_IDEMPOTENCY_KEY="digest-file-run",
    )
    assert submitted.status_code == 201, submitted.data
    run = AnalysisRun.objects.get(pk=submitted.data["id"])
    item = run.request_payload["input_resource_manifest"]["files"][0]
    assert item["verification"] == "identity_v2"
    assert item["ctime_ns"] > 0
    assert "sha256" not in item

    path = rawdata / item["relative_path"]
    original_stat = path.stat()
    path.write_bytes(b"x" * original_stat.st_size)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    with pytest.raises(RuntimeError, match="排队后发生变化"):
        _verify_run_resource_manifests(run)


@pytest.mark.django_db
def test_preflight_reports_transient_resource_wait(
    integration_workspace, monkeypatch, settings
):
    _, _, _, client = _token_client()
    version = _workflow_version()
    monkeypatch.setattr("workflows.integration_api._available_memory_bytes", lambda: 0)
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 8

    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        _submission(version),
        format="json",
    )

    assert response.status_code == 200, response.data
    assert response.data["ready"] is False
    assert response.data["submission_allowed"] is True
    assert response.data["waiting_for"] == ["execution_memory"]


@pytest.mark.django_db
def test_preflight_validates_and_snapshots_database_catalog(
    integration_workspace, settings
):
    _, database, _ = integration_workspace
    _, _, _, client = _token_client()
    version = _workflow_version()
    contract = dict(version.interface_contract)
    contract["database"] = {
        "required": True,
        "panel_required": True,
        "allowed_reference_ids": ["hg19"],
        "allowed_panel_ids": ["panel-a"],
    }
    WorkflowVersion.objects.filter(pk=version.pk).update(interface_contract=contract)
    version.refresh_from_db()
    catalog_path = database / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "references": [
                    {
                        "id": "hg19",
                        "required": [
                            {"path": "hg19/reference.fa", "kind": "file"},
                            {"path": "hg19/bundle", "kind": "directory"},
                        ],
                    }
                ],
                "panels": [
                    {
                        "id": "panel-a",
                        "reference": "hg19",
                        "required": [{"path": "panels/a.bed", "kind": "file"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings.ANALYSIS_DATABASE_CATALOG = catalog_path
    body = _submission(version)
    body["database"] = {"reference_id": "hg19", "panel_id": "panel-a"}

    missing = client.post(
        "/api/v1/integration/analysis-runs/preflight", body, format="json"
    )
    assert missing.status_code == 400
    assert missing.data["error"]["code"] == "ANALYSIS_DATABASE_INCOMPLETE"
    assert {item["path"] for item in missing.data["error"]["details"]["missing"]} == {
        "hg19/reference.fa",
        "hg19/bundle",
        "panels/a.bed",
    }

    (database / "hg19").mkdir()
    (database / "hg19/bundle").mkdir()
    (database / "panels").mkdir()
    (database / "hg19/reference.fa").write_text(">chr1\nACGT\n", encoding="utf-8")
    (database / "hg19/bundle/index.txt").write_text("index\n", encoding="utf-8")
    (database / "panels/a.bed").write_text("chr1\t0\t4\n", encoding="utf-8")
    ready = client.post(
        "/api/v1/integration/analysis-runs/preflight", body, format="json"
    )
    assert ready.status_code == 200, ready.data
    assert ready.data["ready"] is True
    assert {item["check"] for item in ready.data["checks"]} >= {
        "database_reference",
        "database_panel",
    }
    assert ready.data["resource_manifest"]["database_selection"] == {
        "reference_id": "hg19",
        "panel_id": "panel-a",
    }

    submitted = client.post(
        "/api/v1/integration/analysis-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="okb-run-1",
    )
    assert submitted.status_code == 201, submitted.data
    run = AnalysisRun.objects.get(pk=submitted.data["id"])
    assert run.request_payload["database_selection"]["reference_id"] == "hg19"
    assert run.request_payload["reference_resource_manifest"]["resources"]
    assert run.request_payload["panel_resource_manifest"]["resources"]

    with patch(
        "workflows.integration_outputs._directory_manifest_isolated",
        side_effect=ValueError("snapshot policy exceeded"),
    ):
        rejected = client.post(
            "/api/v1/integration/analysis-runs/preflight", body, format="json"
        )
    assert rejected.status_code == 400
    assert rejected.data["error"]["code"] == "ANALYSIS_RESOURCE_UNSUPPORTED"


@pytest.mark.django_db
def test_preflight_rejects_symlink_escape_wrong_mate_digest_and_nested_identity(
    integration_workspace,
    tmp_path: Path,
):
    rawdata, _, _ = integration_workspace
    _, _, _, client = _token_client()
    version = _workflow_version()

    outside = tmp_path / "outside.fastq.gz"
    _write_fastq(outside, 1)
    (rawdata / "escape.fastq.gz").symlink_to(outside)
    escaped = _submission(version)
    escaped["inputs"]["read1"]["relative_path"] = "escape.fastq.gz"
    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        escaped,
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "MANAGED_RESOURCE_ESCAPE"

    absolute = _submission(version)
    absolute["inputs"]["read1"]["relative_path"] = str(outside)
    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        absolute,
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "MANAGED_RESOURCE_PATH_INVALID"

    _write_fastq(rawdata / "S001_R1.fastq.gz", 2)
    wrong_mate = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        _submission(version),
        format="json",
    )
    assert wrong_mate.status_code == 400
    assert wrong_mate.data["error"]["code"] == "FASTQ_MATE_INVALID"

    _write_fastq(rawdata / "S001_R1.fastq.gz", 1)
    digest_mismatch = _submission(version)
    digest_mismatch["workflow"]["expected_source_digest"] = "sha256:" + "0" * 64
    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        digest_mismatch,
        format="json",
    )
    assert response.status_code == 409
    assert response.data["error"]["code"] == "WORKFLOW_VERSION_CHANGED"

    clinical = _submission(version)
    clinical["metadata"] = {"context": {"patientName": "sensitive"}}
    response = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        clinical,
        format="json",
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "METADATA_CONTAINS_CLINICAL_IDENTITY"

    WorkflowVersion.objects.filter(pk=version.pk).update(compiled_bundle={})
    not_runnable = client.post(
        "/api/v1/integration/analysis-runs/preflight",
        _submission(version),
        format="json",
    )
    assert not_runnable.status_code == 400
    assert not_runnable.data["error"]["code"] == "WORKFLOW_VERSION_NOT_RUNNABLE"


@pytest.mark.django_db
def test_idempotent_submission_find_cancel_and_status_version(integration_workspace):
    account, _, _, client = _token_client()
    version = _workflow_version()
    body = _submission(version)
    headers = {"HTTP_IDEMPOTENCY_KEY": "okb-run-1"}

    created = client.post("/api/v1/integration/analysis-runs", body, format="json", **headers)
    assert created.status_code == 201, created.data
    run_id = created.data["id"]
    assert AnalysisRun.objects.filter(service_account=account).count() == 1

    replayed = client.post("/api/v1/integration/analysis-runs", body, format="json", **headers)
    assert replayed.status_code == 200
    assert replayed["Idempotency-Replayed"] == "true"
    assert replayed.data["id"] == run_id
    assert AnalysisRun.objects.filter(service_account=account).count() == 1

    changed = _submission(version)
    changed["metadata"]["product_code"] = "OTHER"
    conflict = client.post("/api/v1/integration/analysis-runs", changed, format="json", **headers)
    assert conflict.status_code == 409
    assert conflict.data["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    found = client.get(
        "/api/v1/integration/analysis-runs/by-external-ref",
        {"external_run_id": "okb-run-1"},
    )
    assert found.status_code == 200
    assert found.data["id"] == run_id

    canceled = client.post(f"/api/v1/integration/analysis-runs/{run_id}/cancel", {}, format="json")
    assert canceled.status_code == 200
    assert canceled.data["status"] == "canceled"
    assert canceled.data["status_version"] == 2
    repeated = client.post(f"/api/v1/integration/analysis-runs/{run_id}/cancel", {}, format="json")
    assert repeated.data["status_version"] == 2


@pytest.mark.django_db
def test_retry_is_new_record_and_rejects_changed_inputs(integration_workspace):
    account, _, _, client = _token_client()
    version = _workflow_version()
    created = client.post(
        "/api/v1/integration/analysis-runs",
        _submission(version),
        format="json",
        HTTP_IDEMPOTENCY_KEY="original",
    )
    original = AnalysisRun.objects.get(pk=created.data["id"])
    original.status = AnalysisRun.Status.FAILED
    original.error = "fixture failure"
    original.save(update_fields=["status", "error", "updated_at"])

    retry_body = {
        "external_ref": {
            "client_id": "okb",
            "external_run_id": "okb-run-retry-1",
            "external_analysis_id": "analysis-1",
        }
    }
    retried = client.post(
        f"/api/v1/integration/analysis-runs/{original.id}/retry",
        retry_body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="retry-1",
    )
    assert retried.status_code == 201, retried.data
    retry = AnalysisRun.objects.get(pk=retried.data["id"])
    assert retry.retry_of == original
    assert retry.source_digest == original.source_digest
    assert retry.input_values == original.input_values
    assert retry.service_account == account
    original.refresh_from_db()
    assert original.status == AnalysisRun.Status.FAILED

    current_payload = json.loads(json.dumps(original.request_payload))
    legacy_payload = json.loads(json.dumps(current_payload))
    legacy_file = legacy_payload["input_resource_manifest"]["files"][0]
    legacy_file.pop("ctime_ns")
    legacy_file.pop("sha256", None)
    original.request_payload = legacy_payload
    original.save(update_fields=["request_payload", "updated_at"])
    outdated = client.post(
        f"/api/v1/integration/analysis-runs/{original.id}/retry",
        {
            "external_ref": {
                "client_id": "okb",
                "external_run_id": "okb-run-retry-outdated",
            }
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="retry-outdated",
    )
    assert outdated.status_code == 409
    assert outdated.data["error"]["code"] == "ANALYSIS_RESOURCE_MANIFEST_OUTDATED"
    original.request_payload = current_payload
    original.save(update_fields=["request_payload", "updated_at"])

    rawdata, _, _ = integration_workspace
    digest_payload = json.loads(json.dumps(current_payload))
    digest_payload["input_resource_manifest"]["files"][0]["sha256"] = _sha256(
        rawdata / "S001_R1.fastq.gz"
    )
    original.request_payload = digest_payload
    original.save(update_fields=["request_payload", "updated_at"])
    with patch(
        "workflows.integration_outputs.ResourceSnapshotBudget.file_digest",
        side_effect=ResourceSnapshotBudgetError("资源快照超过请求时间上限。"),
    ):
        bounded = client.post(
            f"/api/v1/integration/analysis-runs/{original.id}/retry",
            {
                "external_ref": {
                    "client_id": "okb",
                    "external_run_id": "okb-run-retry-budget",
                }
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="retry-budget",
        )
    assert bounded.status_code == 409
    assert bounded.data["error"]["code"] == "MANAGED_RESOURCE_LIMIT_EXCEEDED"
    original.request_payload = current_payload
    original.save(update_fields=["request_payload", "updated_at"])

    _write_fastq(rawdata / "S001_R1.fastq.gz", 1, read_id="replacement")
    changed_body = {
        "external_ref": {
            "client_id": "okb",
            "external_run_id": "okb-run-retry-2",
        }
    }
    changed = client.post(
        f"/api/v1/integration/analysis-runs/{original.id}/retry",
        changed_body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="retry-2",
    )
    assert changed.status_code == 409
    assert changed.data["error"]["code"] == "ANALYSIS_RESOURCE_CHANGED"


@pytest.mark.django_db
def test_semantic_output_manifest_download_and_integrity(
    integration_workspace, settings
):
    account, _, _, client = _token_client()
    version = _workflow_version()
    run = AnalysisRun.objects.create(
        workflow_version=version,
        service_account=account,
        external_run_id="output-run",
        idempotency_key="output-run",
        workflow_name="integration_smoke",
        sample_id="S001",
        actor="service:okb",
        source_bundle=version.compiled_bundle,
        source_digest=version.compiled_digest,
        status=AnalysisRun.Status.SUCCEEDED,
        request_payload={
            "integration_output_contract": [
                {
                    "key": "integration_smoke.result",
                    "name": "result",
                    "label": "QC result",
                    "semantic_type": "report.qc_tsv",
                    "wdl_type": "File",
                    "required": True,
                },
                {
                    "key": "integration_smoke.note",
                    "name": "note",
                    "label": "Note",
                    "semantic_type": "report.note",
                    "wdl_type": "String",
                    "required": False,
                },
            ]
        },
    )
    _, _, run_root = integration_workspace
    directory = run_root / str(run.id)
    directory.mkdir()
    output = directory / "result.tsv"
    output.write_text("sample\tvalue\nS001\t1\n", encoding="utf-8")
    run.work_directory = str(directory)
    result = {
        "outputs": {
            "integration_smoke.result": str(output),
            "integration_smoke.note": f"stored at {run_root}/internal",
            "integration_smoke.internal_path": "/mnt/nas/private/result.txt",
        }
    }
    manifest, output_status, error = build_output_manifest(run, result)
    assert error is None
    execution_root = Path("/execution/analysis-runs")
    settings.ANALYSIS_RUN_EXECUTION_ROOT = execution_root
    for field in ("path", "source_path"):
        local_path = Path(manifest["items"][0][field])
        manifest["items"][0][field] = str(
            execution_root / local_path.relative_to(run_root)
        )
    run.outputs = result
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["work_directory", "outputs", "output_manifest", "output_status", "updated_at"])

    listed = client.get(f"/api/v1/integration/analysis-runs/{run.id}/outputs")
    assert listed.status_code == 200
    item = listed.data["results"][0]
    assert item["semantic_type"] == "report.qc_tsv"
    assert item["sha256"].startswith("sha256:")
    assert "path" not in item and "identity" not in item
    assert "source_path" not in item and "source_identity" not in item
    assert len(listed.data["results"]) == 2
    assert listed.data["results"][1]["value"] == "stored at <managed-root>/internal"
    assert "private" not in json.dumps(listed.data)
    assert manifest["uncontracted_output_keys"] == ["integration_smoke.internal_path"]

    account.scopes = ["analysis:read"]
    account.save(update_fields=["scopes", "updated_at"])
    forbidden = client.get(item["download_url"])
    assert forbidden.status_code == 403
    assert forbidden.data["error"]["code"] == "SERVICE_SCOPE_REQUIRED"
    account.scopes = ALL_SCOPES
    account.save(update_fields=["scopes", "updated_at"])

    downloaded = client.get(item["download_url"])
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == output.read_bytes()

    output.write_text("tampered\n", encoding="utf-8")
    changed = client.get(item["download_url"])
    assert changed.status_code == 409
    assert changed.data["error"]["code"] == "ANALYSIS_OUTPUT_CHANGED"


@pytest.mark.django_db
def test_integration_incomplete_v2_manifest_keeps_verified_file_downloadable(
    integration_workspace, settings, monkeypatch
):
    account, _, _, client = _token_client()
    version = _workflow_version()
    _, _, runs = integration_workspace
    run_directory = runs / "integration-partial"
    run_directory.mkdir()
    output = run_directory / "result.tsv"
    output.write_text("verified\n", encoding="utf-8")
    settings.ANALYSIS_OUTPUT_VALUE_MAX_BYTES = 16
    run = AnalysisRun.objects.create(
        workflow_version=version,
        service_account=account,
        external_run_id="integration-partial",
        idempotency_key="integration-partial",
        workflow_name="integration_smoke",
        sample_id="S001",
        actor="service:okb",
        source_bundle=version.compiled_bundle,
        source_digest=version.compiled_digest,
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        request_payload={
            "integration_output_contract": [
                {
                    "key": "integration_smoke.result",
                    "wdl_type": "File",
                    "required": True,
                },
                {
                    "key": "integration_smoke.note",
                    "wdl_type": "String",
                    "required": True,
                },
            ]
        },
        outputs={
            "outputs": {
                "integration_smoke.result": str(output),
                "integration_smoke.note": "sensitive-marker-" + "x" * 100,
            }
        },
    )
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "OUTPUT_INTEGRITY_UNVERIFIABLE"
    run.output_manifest = manifest
    run.output_status = output_status
    run.save(update_fields=["output_manifest", "output_status", "updated_at"])
    monkeypatch.setattr(
        "workflows.integration_api._output_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v2 manifest must not read legacy outputs")
        ),
    )

    run_list = client.get("/api/v1/integration/analysis-runs")
    listed = client.get(f"/api/v1/integration/analysis-runs/{run.id}/outputs")
    results = {item["key"]: item for item in listed.data["results"]}
    downloaded = client.get(results["integration_smoke.result"]["download_url"])
    incomplete = client.get(
        f"/api/v1/integration/analysis-runs/{run.id}/outputs/download",
        {"key": "integration_smoke.note"},
    )

    assert run_list.status_code == 200
    assert run_list.data["view"] == "summary"
    assert run_list.data["results"][0]["outputs"] == []
    assert "sensitive-marker" not in str(run_list.data)
    assert results["integration_smoke.result"]["download_url"]
    assert "download_url" not in results["integration_smoke.note"]
    assert results["integration_smoke.note"]["kind"] == "unverifiable"
    assert downloaded.status_code == 200
    assert b"".join(downloaded.streaming_content) == b"verified\n"
    assert incomplete.status_code == 409
    assert incomplete.data["error"]["code"] == "ANALYSIS_OUTPUT_INCOMPLETE"


@pytest.mark.django_db
def test_integration_legacy_schema_v1_output_is_unverified(
    integration_workspace,
):
    _, _, runs = integration_workspace
    account, _, _, client = _token_client()
    version = _workflow_version()
    run_directory = runs / "legacy-integration-output"
    run_directory.mkdir()
    output = run_directory / "result.tsv"
    output.write_text("legacy\n", encoding="utf-8")
    run = AnalysisRun.objects.create(
        workflow_version=version,
        service_account=account,
        external_run_id="legacy-output",
        idempotency_key="legacy-output",
        workflow_name="integration_smoke",
        sample_id="S001",
        actor="service:okb",
        status=AnalysisRun.Status.SUCCEEDED,
        work_directory=str(run_directory),
        outputs={"outputs": {"integration_smoke.result": str(output)}},
        output_manifest={
            "schema_version": 1,
            "items": [
                {
                    "key": "integration_smoke.result",
                    "kind": "file",
                    "path": str(output),
                    "sha256": _sha256(output),
                }
            ],
        },
        output_status=AnalysisRun.OutputStatus.COMPLETE,
    )

    listed = client.get(f"/api/v1/integration/analysis-runs/{run.id}/outputs")
    downloaded = client.get(
        f"/api/v1/integration/analysis-runs/{run.id}/outputs/download",
        {"key": "integration_smoke.result"},
    )

    assert listed.status_code == 200
    assert "download_url" not in listed.data["results"][0]
    assert downloaded.status_code == 409
    assert downloaded.data["error"]["code"] == "ANALYSIS_OUTPUT_UNVERIFIED"

    run.output_manifest = {}
    run.save(update_fields=["output_manifest", "updated_at"])
    downloaded_without_manifest = client.get(
        f"/api/v1/integration/analysis-runs/{run.id}/outputs/download",
        {"key": "integration_smoke.result"},
    )
    assert downloaded_without_manifest.status_code == 409
    assert (
        downloaded_without_manifest.data["error"]["code"]
        == "ANALYSIS_OUTPUT_UNVERIFIED"
    )


@pytest.mark.django_db
def test_required_output_missing_is_distinct_from_execution_failure(integration_workspace):
    account, _, _, _ = _token_client()
    version = _workflow_version()
    run = AnalysisRun.objects.create(
        workflow_version=version,
        service_account=account,
        external_run_id="missing-output",
        idempotency_key="missing-output",
        workflow_name="integration_smoke",
        sample_id="S001",
        actor="service:okb",
        source_bundle=version.compiled_bundle,
        source_digest=version.compiled_digest,
        request_payload={
            "integration_output_contract": [
                {
                    "key": "integration_smoke.result",
                    "semantic_type": "report.qc_tsv",
                    "wdl_type": "File",
                    "required": True,
                }
            ]
        },
    )
    _, _, run_root = integration_workspace
    directory = run_root / str(run.id)
    directory.mkdir()
    run.work_directory = str(directory)
    manifest, output_status, error = build_output_manifest(run, {"outputs": {}})
    assert output_status == AnalysisRun.OutputStatus.INCOMPLETE
    assert error["code"] == "REQUIRED_OUTPUT_MISSING"
    assert manifest["missing_required"][0]["semantic_type"] == "report.qc_tsv"


@pytest.mark.django_db
def test_tool_test_submission_and_library_endpoints(integration_workspace):
    _, _, _, client = _token_client()
    spec = {
        "schema_version": "1.0.0",
        "id": "echo_task",
        "name": "echo_task",
        "display_name": "Echo Task",
        "tool_version": "1.0.0",
        "description": "Shell-only task",
        "container": {"engine": "docker", "image": "ubuntu:24.04"},
        "inputs": [{"name": "message", "wdl_type": "String", "semantic_type": "core.value.string", "required": True}],
        "outputs": [{"name": "result", "wdl_type": "File", "semantic_type": "core.file.any", "capture": {"mode": "path", "value": "result.txt"}}],
        "command": {"shell": "bash", "strict_mode": True, "template": 'printf "%s\\n" "~{message}" > result.txt'},
        "runtime": {"cpu": 1, "memory_gb": 1, "disk_gb": 1},
    }
    tool = ToolVersion.objects.create(
        tool_id=spec["id"],
        version=spec["tool_version"],
        name=spec["display_name"],
        digest=canonical_digest(spec),
        tool_spec=spec,
    )
    SoftwareAsset.objects.create(slug="shell", name="Shell", summary="Runtime notes")
    body = {
        "external_ref": {"client_id": "okb", "external_run_id": "task-test-1"},
        "tool": {"tool_id": tool.tool_id, "version": tool.version, "expected_digest": tool.digest},
        "inputs": {"message": "hello"},
    }
    ready = client.post("/api/v1/integration/tool-test-runs/preflight", body, format="json")
    assert ready.status_code == 200, ready.data
    submitted = client.post(
        "/api/v1/integration/tool-test-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="task-test-1",
    )
    assert submitted.status_code == 201, submitted.data
    assert submitted.data["run_kind"] == "tool_test"
    assert client.get("/api/v1/integration/tools").data["results"][0]["tool_id"] == "echo_task"
    assert client.get("/api/v1/integration/software").data["results"][0]["slug"] == "shell"


@pytest.mark.django_db
def test_tool_test_submission_rolls_back_if_initial_event_fails(integration_workspace):
    account, _, _, client = _token_client()
    spec = {
        "schema_version": "1.0.0",
        "id": "atomic_echo",
        "name": "atomic_echo",
        "display_name": "Atomic echo",
        "tool_version": "1.0.0",
        "description": "Transaction fixture",
        "container": {"engine": "docker", "image": "ubuntu:24.04"},
        "inputs": [{"name": "message", "wdl_type": "String", "semantic_type": "core.value.string", "required": True}],
        "outputs": [{"name": "result", "wdl_type": "File", "semantic_type": "core.file.any", "capture": {"mode": "path", "value": "result.txt"}}],
        "command": {"shell": "bash", "strict_mode": True, "template": 'printf "%s\\n" "~{message}" > result.txt'},
        "runtime": {"cpu": 1, "memory_gb": 1, "disk_gb": 1},
    }
    tool = ToolVersion.objects.create(
        tool_id=spec["id"],
        version=spec["tool_version"],
        name=spec["display_name"],
        digest=canonical_digest(spec),
        tool_spec=spec,
    )
    body = {
        "external_ref": {"client_id": account.client_id, "external_run_id": "atomic-tool-test"},
        "tool": {"tool_id": tool.tool_id, "version": tool.version, "expected_digest": tool.digest},
        "inputs": {"message": "hello"},
    }
    client.raise_request_exception = False

    with patch(
        "workflows.integration_api.AnalysisRunEvent.objects.create",
        side_effect=RuntimeError("event storage unavailable"),
    ):
        response = client.post(
            "/api/v1/integration/tool-test-runs",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY="atomic-tool-test",
        )

    assert response.status_code == 500
    assert not AnalysisRun.objects.filter(
        service_account=account, external_run_id="atomic-tool-test"
    ).exists()


@pytest.mark.django_db
def test_active_cancel_finalizes_after_worker_lease_is_revoked():
    account, _, _, _ = _token_client()
    version = _workflow_version()
    lease = uuid.uuid4()
    run = AnalysisRun.objects.create(
        workflow_version=version,
        service_account=account,
        external_run_id="active-cancel",
        idempotency_key="active-cancel",
        workflow_name="integration_smoke",
        sample_id="S001",
        actor="service:okb",
        source_bundle=version.compiled_bundle,
        source_digest=version.compiled_digest,
        status=AnalysisRun.Status.CANCEL_REQUESTED,
        status_version=3,
        lease_token=lease,
    )
    assert _finalize_cancelled_run(run.pk, lease) is True
    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.CANCELED
    assert run.status_version == 4
    assert run.finished_at is not None
    assert run.lease_token is None


@pytest.mark.django_db
def test_worker_cancel_race_and_stale_lease_have_terminal_structured_state(
    django_capture_on_commit_callbacks,
):
    account, _, _, _ = _token_client()
    version = _workflow_version()
    canceled = AnalysisRun.objects.create(
        workflow_version=version,
        service_account=account,
        external_run_id="cancel-race",
        idempotency_key="cancel-race",
        workflow_name="integration_smoke",
        sample_id="S001",
        actor="service:okb",
        source_bundle=version.compiled_bundle,
        source_digest=version.compiled_digest,
        status=AnalysisRun.Status.CANCEL_REQUESTED,
        status_version=2,
        lease_token=uuid.uuid4(),
    )
    process_analysis_run(canceled)
    canceled.refresh_from_db()
    assert canceled.status == AnalysisRun.Status.CANCELED
    assert canceled.error_code == "ANALYSIS_CANCELED"

    stale = AnalysisRun.objects.create(
        workflow_version=version,
        service_account=account,
        external_run_id="stale-running",
        idempotency_key="stale-running",
        workflow_name="integration_smoke",
        sample_id="S001",
        actor="service:okb",
        source_bundle=version.compiled_bundle,
        source_digest=version.compiled_digest,
        status=AnalysisRun.Status.RUNNING,
        status_version=2,
        lease_token=uuid.uuid4(),
        lease_expires_at=timezone.now() - timedelta(seconds=1),
        work_directory="/managed/run",
    )
    with patch(
        "workflows.analysis_runtime._cleanup_swarm_services_for_run"
    ) as cleanup:
        with django_capture_on_commit_callbacks(execute=True):
            assert claim_next_run() is None
    cleanup.assert_called_once_with(Path("/managed/run"))
    stale.refresh_from_db()
    assert stale.status == AnalysisRun.Status.FAILED
    assert stale.error_code == "ANALYSIS_WORKER_LEASE_LOST"
    assert stale.error_category == "infrastructure"
    assert stale.error_retryable is True


def test_mcp_protocol_advertises_scoped_read_and_write_tools():
    class FakeClient:
        pass

    initialized = handle(
        FakeClient(),
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert initialized["result"]["serverInfo"]["name"] == "bioworkflow-manage"
    listed = handle(FakeClient(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {item["name"] for item in listed["result"]["tools"]}
    assert names == {item["name"] for item in TOOLS}
    assert {"preflight_workflow", "submit_workflow", "preflight_task_test", "submit_task_test"} <= names
    cancel = next(item for item in TOOLS if item["name"] == "cancel_run")
    assert cancel["annotations"]["destructiveHint"] is True

    unknown = handle(
        FakeClient(),
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "not_a_tool", "arguments": {}},
        },
    )
    assert unknown["error"]["code"] == -32602

    missing = handle(
        FakeClient(),
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_run", "arguments": {}},
        },
    )
    assert missing["result"]["isError"] is True


def test_mcp_submit_tools_preserve_idempotency_and_database_selection():
    calls = []

    class FakeClient:
        def request(self, method, path, *, body=None, headers=None):
            calls.append((method, path, body, headers))
            return {"id": "run-1"}

    arguments = {
        "external_ref": {"client_id": "okb", "external_run_id": "run-1"},
        "idempotency_key": "run-1",
        "workflow": {
            "source_type": "workflow_version",
            "version_id": 1,
            "expected_source_digest": "sha256:digest",
        },
        "subject": {"sample_id": "S001"},
        "inputs": {},
        "database": {"reference_id": "hg19"},
    }

    assert tool_call(FakeClient(), "submit_workflow", arguments) == {"id": "run-1"}
    method, path, body, headers = calls[0]
    assert (method, path) == ("POST", "/api/v1/integration/analysis-runs")
    assert body["database"] == {"reference_id": "hg19"}
    assert "idempotency_key" not in body
    assert headers == {"Idempotency-Key": "run-1"}


def test_mcp_analysis_product_discovery_and_submission():
    calls = []

    class FakeClient:
        def request(self, method, path, *, body=None, headers=None):
            calls.append((method, path, body, headers))
            return {"ok": True}

    client = FakeClient()
    assert tool_call(client, "list_analysis_products", {}) == {"ok": True}
    assert tool_call(
        client,
        "get_analysis_product",
        {"analysis_code": "dna-panel", "contract_version": "1.0.0"},
    ) == {"ok": True}
    assert tool_call(
        client,
        "submit_workflow",
        {
            "external_ref": {"client_id": "okb", "external_run_id": "run-1"},
            "idempotency_key": "run-1",
            "analysis_product": {
                "analysis_code": "dna-panel",
                "contract_version": "1.0.0",
            },
            "subject": {"sample_id": "S001"},
            "inputs": {},
        },
    ) == {"ok": True}

    assert calls[0][0:2] == ("GET", "/api/v1/integration/analysis-products")
    assert calls[1][0:2] == (
        "GET",
        "/api/v1/integration/analysis-products/dna-panel/versions/1.0.0",
    )
    assert calls[2][0:2] == ("POST", "/api/v1/integration/analysis-runs")
    assert calls[2][2]["analysis_product"]["analysis_code"] == "dna-panel"
    assert calls[2][3] == {"Idempotency-Key": "run-1"}


@pytest.mark.django_db
def test_openapi_contract_covers_every_integration_route():
    payload = json.loads(
        (Path(__file__).parents[2] / "schemas" / "integration-openapi-v1.json").read_text(
            encoding="utf-8"
        )
    )
    paths = payload["paths"]
    assert {
        "/openapi",
        "/analysis-products",
        "/analysis-products/{analysis_code}/versions/{contract_version}",
        "/workflow-versions",
        "/workflow-versions/{version_id}",
        "/analysis-runs/preflight",
        "/analysis-runs",
        "/analysis-runs/by-external-ref",
        "/analysis-runs/batch-status",
        "/analysis-runs/{run_id}",
        "/analysis-runs/{run_id}/events",
        "/analysis-runs/{run_id}/cancel",
        "/analysis-runs/{run_id}/retry",
        "/analysis-runs/{run_id}/outputs",
        "/analysis-runs/{run_id}/outputs/download",
        "/tool-test-runs/preflight",
        "/tool-test-runs",
        "/tools",
        "/software",
    } <= set(paths)
    workflow_ref = payload["components"]["schemas"]["WorkflowVersionRef"]
    assert workflow_ref.get("additionalProperties", True) is not False
    input_reference = payload["components"]["schemas"]["InputReference"]
    assert {item["$ref"] for item in input_reference["oneOf"]} == {
        "#/components/schemas/ManagedResource",
        "#/components/schemas/S3ObjectReference",
    }
    s3_reference = payload["components"]["schemas"]["S3ObjectReference"]
    assert s3_reference["additionalProperties"] is False
    assert {"profile", "bucket", "key", "size", "sha256"} <= set(
        s3_reference["required"]
    )
    input_value = payload["components"]["schemas"]["InputValue"]
    assert input_value["anyOf"][0]["$ref"] == "#/components/schemas/InputReference"
    assert input_value["anyOf"][1]["items"]["$ref"] == (
        "#/components/schemas/InputValue"
    )
    for schema_name in (
        "AnalysisSubmission",
        "AnalysisPreflight",
        "ToolTestPreflight",
        "ToolTestSubmission",
    ):
        assert payload["components"]["schemas"][schema_name]["properties"]["inputs"][
            "additionalProperties"
        ]["$ref"] == "#/components/schemas/InputValue"
    assert "404" in paths["/analysis-runs/preflight"]["post"]["responses"]
    assert "404" in paths["/analysis-runs"]["post"]["responses"]
    assert "analysisRunTerminal" in payload["webhooks"]
    assert (
        payload["webhooks"]["analysisRunTerminal"]["post"]["requestBody"]
        ["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/AnalysisRunTerminalEvent"
    )
    served = APIClient().get("/api/v1/integration/openapi")
    assert served.status_code == 200
    assert served.data["info"]["version"] == payload["info"]["version"]

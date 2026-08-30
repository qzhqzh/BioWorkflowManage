from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest
from clients.python_mes_client import ConnectorClient as MesConnectorClient
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from reference_connector import (
    ConnectorConflictError,
    ConnectorError,
    ConnectorIntegrityError,
    ConnectorStore,
    IntegrationClient,
    IntegrationTransportError,
    MappingConfig,
    MappingError,
    ReferenceConnector,
    SubmissionUncertainError,
    TransportResponse,
    UrllibTransport,
)
from reference_connector.config import Runtime, ServerConfig, load_runtime
from reference_connector.mapping import canonical_digest
from reference_connector.server import ReferenceConnectorHTTPServer
from scripts.validate_reference_connector_contract import (
    load_json,
    validate_reference_connector_contract,
)
from workflows.integration_outputs import build_output_manifest, public_output_manifest
from workflows.integration_tokens import issue_service_token
from workflows.models import (
    AnalysisProductVersion,
    AnalysisRun,
    ServiceAccount,
)


pytestmark = pytest.mark.usefixtures("auth_disabled")

CONTRACT_DIGEST = "sha256:" + "1" * 64
WEBHOOK_SECRET_BYTES = b"reference-connector-webhook-key!"
WEBHOOK_SECRET = base64.urlsafe_b64encode(WEBHOOK_SECRET_BYTES).rstrip(b"=").decode()


def _json_response(status_code: int, value: Any) -> TransportResponse:
    return TransportResponse(
        status_code,
        {"Content-Type": "application/json"},
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


def _mapping(*, analysis_code="reference-analysis", expected_digest=CONTRACT_DIGEST):
    return MappingConfig.from_dict(
        {
            "schema_version": "1.0.0",
            "client_id": "mes-reference",
            "analysis_product": {
                "analysis_code": analysis_code,
                "contract_version": "1.0.0",
                "expected_contract_digest": expected_digest,
            },
            "fields": {
                "external_run_id": "order.execution_id",
                "external_analysis_id": "order.analysis_id",
                "sample_id": "sample.code",
            },
            "inputs": {
                "probe": {
                    "path": "files.probe",
                    "kind": "managed_file",
                    "root_alias": "rawdata",
                }
            },
            "metadata": {
                "product_code": {"path": "product.code", "required": True},
                "batch_id": {"path": "batch.id", "required": False},
            },
            "database": {},
        }
    )


def _order(external_run_id="MES-ORDER-001"):
    return {
        "order": {
            "execution_id": external_run_id,
            "analysis_id": "ANALYSIS-001",
        },
        "sample": {
            "code": "S001",
            "patient_name": "must-not-leave-the-connector",
        },
        "files": {"probe": "incoming/S001.txt"},
        "product": {"code": "PANEL-001", "customer_private_field": "local-only"},
        "batch": {"id": "BATCH-001"},
    }


class SimulatedAnalysisNodeTransport:
    def __init__(self) -> None:
        self.run: dict[str, Any] | None = None
        self.submission: dict[str, Any] | None = None
        self.submit_calls = 0
        self.fail_submit_after_commit = False
        self.submit_response_failures = 0
        self.find_failures = 0
        self.fail_conflict_response_once = False
        self.redirect_products = False
        self.product_ready = True
        self.output_content = b"sample\tvalue\nS001\t1\n"
        self.download_content = self.output_content
        self.second_output_content = b"second-output\n"
        self.second_download_content = self.second_output_content
        self.include_second_output = False
        self.omit_value = False
        self.export: dict[str, Any] | None = None
        self.export_submit_calls = 0
        self.export_lookup_calls = 0
        self.fail_export_lookup = False
        self.ack_count = 0
        self.openapi_calls = 0
        self.openapi_version = "1.5.0"
        self.openapi_has_request_digest = True

    @staticmethod
    def _error(status_code: int, code: str) -> TransportResponse:
        return _json_response(
            status_code,
            {
                "error": {
                    "code": code,
                    "category": "validation",
                    "message": code,
                    "retryable": False,
                    "details": {},
                }
            },
        )

    def _create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        external = payload["external_ref"]
        return {
            "id": str(uuid.uuid4()),
            "request_digest": canonical_digest(
                {"kind": "workflow", "request": payload}
            ),
            "external_ref": external,
            "analysis_product": {
                "analysis_code": "reference-analysis",
                "contract_version": "1.0.0",
                "contract_digest": CONTRACT_DIGEST,
            },
            "status": "queued",
            "status_version": 1,
            "execution_status": "queued",
            "output_status": "pending",
            "progress": 0,
            "error": None,
            "outputs": [],
        }

    def complete_run(
        self,
        *,
        corrupt_download: bool = False,
        corrupt_second_download: bool = False,
    ) -> None:
        assert self.run is not None
        self.run.update(
            {
                "status": "succeeded",
                "status_version": 4,
                "execution_status": "succeeded",
                "output_status": "complete",
                "progress": 100,
            }
        )
        self.download_content = (
            b"corrupt\n" if corrupt_download else self.output_content
        )
        self.second_download_content = (
            b"x" * len(self.second_output_content)
            if corrupt_second_download
            else self.second_output_content
        )

    def complete_export(self) -> None:
        assert self.export is not None
        relative_path = f"{self.export['run_id']}/{self.export['id']}/result.tsv"
        manifest = {
            "schema_version": 1,
            "export_id": self.export["id"],
            "run_id": self.export["run_id"],
            "source_manifest_digest": "sha256:" + "2" * 64,
            "target": {
                "type": "managed_directory",
                "profile": "mes-results",
                "root_alias": "mes-results",
            },
            "completed_at": "2026-08-30T00:00:00+00:00",
            "items": [
                {
                    "key": "reference.result",
                    "name": "result",
                    "semantic_type": "report.qc_tsv",
                    "wdl_type": "File",
                    "required": True,
                    "kind": "file",
                    "filename": "result.tsv",
                    "size": len(self.output_content),
                    "sha256": "sha256:"
                    + hashlib.sha256(self.output_content).hexdigest(),
                    "content_type": "text/tab-separated-values",
                    "destination": {
                        "type": "managed_directory",
                        "profile": "mes-results",
                        "root_alias": "mes-results",
                        "relative_path": relative_path,
                        "uri": f"managed://mes-results/{relative_path}",
                        "size": len(self.output_content),
                        "sha256": "sha256:"
                        + hashlib.sha256(self.output_content).hexdigest(),
                    },
                }
            ],
            "summary": {
                "file_count": 1,
                "item_count": 1,
                "total_bytes": len(self.output_content),
            },
        }
        self.export.update(
            {
                "state": "succeeded",
                "manifest": manifest,
                "manifest_digest": canonical_digest(manifest),
                "completed_at": "2026-08-30T00:00:00+00:00",
            }
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del timeout_seconds
        assert headers["Authorization"] == "Bearer test-token"
        parsed = urlsplit(url)
        prefix = "/api/v1/integration/"
        assert parsed.path.startswith(prefix)
        path = parsed.path[len(prefix) :]
        payload = json.loads(body) if body is not None else None
        if method == "GET" and path == "openapi":
            self.openapi_calls += 1
            required = ["id"]
            properties: dict[str, Any] = {"id": {"type": "string"}}
            if self.openapi_has_request_digest:
                required.append("request_digest")
                properties["request_digest"] = {
                    "type": "string",
                    "pattern": r"^sha256:[0-9a-f]{64}$",
                }
            return _json_response(
                200,
                {
                    "openapi": "3.1.0",
                    "info": {"version": self.openapi_version},
                    "components": {
                        "schemas": {
                            "AnalysisRun": {
                                "type": "object",
                                "required": required,
                                "properties": properties,
                            }
                        }
                    },
                },
            )
        if method == "GET" and path == "analysis-products":
            if self.redirect_products:
                return TransportResponse(302, {"Location": "https://other.example"}, b"")
            return _json_response(
                200,
                {
                    "results": [
                        {
                            "analysis_code": "reference-analysis",
                            "contract_version": "1.0.0",
                            "contract_digest": CONTRACT_DIGEST,
                            "ready": self.product_ready,
                        }
                    ]
                },
            )
        if method == "GET" and path == "analysis-products/reference-analysis/versions/1.0.0":
            return _json_response(
                200,
                {
                    "analysis_code": "reference-analysis",
                    "contract_version": "1.0.0",
                    "contract_digest": CONTRACT_DIGEST,
                    "ready": self.product_ready,
                    "blockers": [] if self.product_ready else ["deactivated"],
                },
            )
        if method == "POST" and path == "analysis-runs/preflight":
            assert payload["analysis_product"]["analysis_code"] == "reference-analysis"
            return _json_response(
                200,
                {
                    "ready": True,
                    "submission_allowed": True,
                    "checks": [],
                    "waiting_for": [],
                },
            )
        if method == "POST" and path == "analysis-runs":
            self.submit_calls += 1
            if self.run is None:
                self.submission = payload
                self.run = self._create_run(payload)
            elif self.submission != payload:
                if self.fail_conflict_response_once:
                    self.fail_conflict_response_once = False
                    raise IntegrationTransportError("simulated lost conflict response")
                return self._error(409, "IDEMPOTENCY_CONFLICT")
            if self.fail_submit_after_commit:
                self.fail_submit_after_commit = False
                raise IntegrationTransportError("simulated response timeout")
            if self.submit_response_failures:
                self.submit_response_failures -= 1
                raise IntegrationTransportError("simulated repeated response timeout")
            return _json_response(201 if self.submit_calls == 1 else 200, self.run)
        if method == "GET" and path == "analysis-runs/by-external-ref":
            if self.find_failures:
                self.find_failures -= 1
                raise IntegrationTransportError("simulated lookup timeout")
            external_run_id = parse_qs(parsed.query).get("external_run_id", [""])[0]
            if self.run is None or self.run["external_ref"]["external_run_id"] != external_run_id:
                return self._error(404, "NOT_FOUND")
            return _json_response(200, self.run)
        if method == "GET" and path.startswith("analysis-runs/") and path.endswith("/outputs"):
            assert self.run is not None
            digest = "sha256:" + hashlib.sha256(self.output_content).hexdigest()
            results = [
                {
                    "key": "reference.result",
                    "name": "result",
                    "semantic_type": "report.qc_tsv",
                    "wdl_type": "File",
                    "required": True,
                    "kind": "file",
                    "filename": "result.tsv",
                    "size": len(self.output_content),
                    "sha256": digest,
                    "content_type": "text/tab-separated-values",
                    "download_url": f"/api/v1/integration/analysis-runs/{self.run['id']}/outputs/download?key=reference.result",
                },
                {
                    "key": "reference.summary",
                    "name": "summary",
                    "semantic_type": "report.qc_summary",
                    "wdl_type": "Object",
                    "required": False,
                    "kind": "value",
                    **(
                        {}
                        if self.omit_value
                        else {"value": {"sample_count": 1, "passed": True}}
                    ),
                },
            ]
            if self.include_second_output:
                second_digest = "sha256:" + hashlib.sha256(
                    self.second_output_content
                ).hexdigest()
                results.insert(
                    1,
                    {
                        "key": "reference.second",
                        "name": "second",
                        "semantic_type": "report.secondary",
                        "wdl_type": "File",
                        "required": True,
                        "kind": "file",
                        "filename": "second.txt",
                        "size": len(self.second_output_content),
                        "sha256": second_digest,
                        "content_type": "text/plain",
                        "download_url": f"/api/v1/integration/analysis-runs/{self.run['id']}/outputs/download?key=reference.second",
                    },
                )
            return _json_response(
                200,
                {
                    "run_id": self.run["id"],
                    "execution_status": self.run["status"],
                    "output_status": self.run["output_status"],
                    "error": None,
                    "results": results,
                },
            )
        if method == "GET" and path.startswith("analysis-runs/") and path.endswith("/outputs/download"):
            output_key = parse_qs(parsed.query).get("key", [""])[0]
            content = (
                self.second_download_content
                if output_key == "reference.second"
                else self.download_content
            )
            if len(content) > max_response_bytes:
                raise IntegrationTransportError("simulated response limit")
            return TransportResponse(200, {"Content-Type": "text/plain"}, content)
        if method == "GET" and path.startswith("analysis-runs/") and "/" not in path.removeprefix("analysis-runs/"):
            return _json_response(200, self.run)
        if method == "POST" and path.startswith("analysis-runs/") and path.endswith("/artifact-exports"):
            assert self.run is not None
            self.export_submit_calls += 1
            if self.export is None:
                self.export = {
                    "id": str(uuid.uuid4()),
                    "run_id": self.run["id"],
                    "state": "pending",
                    "target": {
                        "type": "managed_directory",
                        "profile": payload["target"]["profile"],
                        "root_alias": "mes-results",
                    },
                    "source_manifest_digest": "sha256:" + "2" * 64,
                    "manifest_digest": None,
                    "manifest": None,
                    "requires_ack": payload["requires_ack"],
                    "acknowledged_at": None,
                }
            return _json_response(201, self.export)
        if method == "GET" and path.startswith("artifact-exports/"):
            self.export_lookup_calls += 1
            if self.fail_export_lookup:
                raise IntegrationTransportError("simulated export lookup outage")
            return _json_response(200, self.export)
        if method == "POST" and path.endswith("/acknowledge"):
            assert self.export is not None
            assert payload["manifest_digest"] == self.export["manifest_digest"]
            if not self.export["acknowledged_at"]:
                self.ack_count += 1
                self.export["acknowledged_at"] = "2026-08-30T00:01:00+00:00"
            return _json_response(200, self.export)
        raise AssertionError(f"Unhandled simulated request: {method} {url}")


class SimulatedMESService:
    def __init__(self, connector: ReferenceConnector) -> None:
        self.connector = connector
        self.result_receipts: dict[str, str] = {}
        self.result_ingest_count = 0

    def submit(self, order: dict[str, Any]) -> dict[str, Any]:
        return self.connector.submit_order(order)

    def ingest_results(self, external_run_id: str) -> dict[str, Any]:
        result = self.connector.collect_results(external_run_id)
        digest = result["result_digest"]
        existing = self.result_receipts.get(external_run_id)
        if existing not in {None, digest}:
            raise ConnectorConflictError("模拟 MES 检测到结果清单冲突。")
        if existing is None:
            self.result_receipts[external_run_id] = digest
            self.result_ingest_count += 1
        return result


@pytest.fixture
def simulated_connector(tmp_path: Path):
    transport = SimulatedAnalysisNodeTransport()
    connector = ReferenceConnector(
        mapping=_mapping(),
        client=IntegrationClient(
            "http://127.0.0.1/api/v1/integration",
            "test-token",
            transport=transport,
        ),
        store=ConnectorStore(tmp_path / "state" / "connector.sqlite3"),
        webhook_secret=WEBHOOK_SECRET,
        result_directory=tmp_path / "results",
    )
    return connector, transport


def _signed_event(payload: dict[str, Any], *, timestamp: int | None = None):
    timestamp = int(time.time()) if timestamp is None else timestamp
    delivery_id = str(uuid.uuid4())
    event_id = str(payload["event_id"])
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signed = b".".join(
        [
            delivery_id.encode(),
            event_id.encode(),
            str(timestamp).encode(),
            body,
        ]
    )
    signature = "v1=" + hmac.new(
        WEBHOOK_SECRET_BYTES,
        signed,
        hashlib.sha256,
    ).hexdigest()
    return (
        {
            "X-BioWorkflow-Delivery-ID": delivery_id,
            "X-BioWorkflow-Event-ID": event_id,
            "X-BioWorkflow-Timestamp": str(timestamp),
            "X-BioWorkflow-Secret-Version": "1",
            "X-BioWorkflow-Signature": signature,
        },
        body,
    )


def _terminal_event(run: dict[str, Any], *, version: int, status: str):
    output_status = "complete" if status == "succeeded" else "unavailable"
    return {
        "schema_version": "1.0.0",
        "event_id": str(uuid.uuid4()),
        "event_type": "analysis.run.terminal",
        "occurred_at": "2026-08-30T00:00:00+00:00",
        "data": {
            "run_id": run["id"],
            "run_kind": "workflow",
            "external_ref": run["external_ref"],
            "analysis_product": run.get("analysis_product"),
            "status": status,
            "status_version": version,
            "output_status": output_status,
            "finished_at": "2026-08-30T00:00:00+00:00",
            "error": None,
            "links": {"run": "/run", "outputs": "/outputs"},
        },
    }


def test_mapping_keeps_customer_fields_inside_connector():
    mapped = _mapping().map_order(_order())

    serialized = json.dumps(mapped.submission, ensure_ascii=False)
    assert mapped.submission["metadata"] == {
        "product_code": "PANEL-001",
        "batch_id": "BATCH-001",
    }
    assert "patient_name" not in serialized
    assert "customer_private_field" not in serialized
    assert mapped.submission["inputs"]["probe"] == {
        "root_alias": "rawdata",
        "relative_path": "incoming/S001.txt",
    }

    unsafe = _order()
    unsafe["files"]["probe"] = "../outside.txt"
    with pytest.raises(MappingError, match="安全"):
        _mapping().map_order(unsafe)

    invalid_mapping = {
        **_mapping().__dict__,
        "metadata_mappings": {
            "product_code": {"path": "product", "required": True}
        },
    }
    with pytest.raises(MappingError, match="scalar"):
        MappingConfig(**invalid_mapping).map_order(_order())


@pytest.mark.parametrize(
    "unsafe_reference",
    [
        {"secretAccessKey": "must-not-leave"},
        {"credentials": {"apiKey": "must-not-leave"}},
        {
            "root_alias": "rawdata",
            "relative_path": "incoming/S001.txt",
            "sk": "must-not-leave",
        },
        {
            "type": "https://storage.example/result?sig=must-not-leave",
            "root_alias": "rawdata",
            "relative_path": "incoming/S001.txt",
        },
        "https://storage.example/result?X-Amz-Signature=must-not-leave",
        "https://customer.internal/private",
    ],
)
def test_mapping_rejects_credential_aliases_and_signed_urls(unsafe_reference):
    mapping = MappingConfig(
        **{
            **_mapping().__dict__,
            "input_mappings": {
                "probe": {"path": "files.probe", "kind": "reference"}
            },
        }
    )
    order = _order()
    order["files"]["probe"] = unsafe_reference

    with pytest.raises(MappingError, match="凭据"):
        mapping.map_order(order)


def test_reference_connector_contract_profile_detects_openapi_drift():
    root = Path(__file__).resolve().parents[2]
    openapi = load_json(root / "schemas" / "integration-openapi-v1.json")
    profile = load_json(
        root / "examples" / "reference_connector" / "contract-surface.json"
    )
    assert validate_reference_connector_contract(openapi, profile).startswith("sha256:")

    changed = json.loads(json.dumps(openapi))
    changed["paths"]["/analysis-runs"]["post"]["operationId"] = "driftedOperation"
    with pytest.raises(AssertionError, match="operationId drift"):
        validate_reference_connector_contract(changed, profile)

    changed = json.loads(json.dumps(openapi))
    changed["paths"]["/openapi"]["get"]["operationId"] = "driftedOpenAPI"
    with pytest.raises(AssertionError, match="operationId drift"):
        validate_reference_connector_contract(changed, profile)

    changed = json.loads(json.dumps(openapi))
    del changed["paths"]["/openapi"]
    with pytest.raises(AssertionError, match="operation is missing"):
        validate_reference_connector_contract(changed, profile)

    for schema_name in ("AnalysisProductList", "ArtifactDeliveryManifest"):
        changed = json.loads(json.dumps(openapi))
        changed["components"]["schemas"][schema_name]["required"] = []
        with pytest.raises(AssertionError, match="contract drift"):
            validate_reference_connector_contract(changed, profile)

    output_response = openapi["paths"]["/analysis-runs/{run_id}/outputs"]["get"][
        "responses"
    ]["200"]
    assert output_response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AnalysisOutputList"
    }
    assert "directory" in openapi["components"]["schemas"]["AnalysisOutputItem"][
        "properties"
    ]["kind"]["enum"]


def test_directory_output_is_represented_by_the_public_contract():
    run = SimpleNamespace(
        id=uuid.uuid4(),
        output_status="complete",
        output_manifest={
            "schema_version": 1,
            "integrity_version": 2,
            "items": [
                {
                    "key": "reference.bundle",
                    "name": "bundle",
                    "semantic_type": "report.bundle",
                    "wdl_type": "Directory",
                    "required": True,
                    "kind": "directory",
                    "digest": "sha256:" + "1" * 64,
                    "entry_count": 2,
                }
            ],
        },
    )

    assert public_output_manifest(run)[0]["kind"] == "directory"


def test_reference_connector_example_config_loads_only_with_valid_secrets(
    tmp_path: Path,
):
    root = Path(__file__).resolve().parents[2]
    document = json.loads(
        (
            root / "examples" / "reference_connector" / "config.example.json"
        ).read_text(encoding="utf-8")
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    environment = {
        "BIOWORKFLOW_TOKEN": "test-token",
        "BIOWORKFLOW_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "REFERENCE_CONNECTOR_INBOUND_TOKEN": "i" * 32,
    }

    runtime = load_runtime(config_path, environ=environment)

    assert runtime.connector.mapping.analysis_code == "dna-panel"
    assert runtime.inbound_token == "i" * 32
    assert runtime.connector.store.path == tmp_path / "state" / "connector.sqlite3"
    assert runtime.connector.result_directory == tmp_path / "results"
    smoke_document = json.loads(
        (
            root
            / "examples"
            / "reference_connector"
            / "config.analysis-node-smoke.example.json"
        ).read_text(encoding="utf-8")
    )
    smoke_document["state"] = {
        "database": "smoke-state/connector.sqlite3",
        "result_directory": "smoke-results",
    }
    smoke_path = tmp_path / "smoke-config.json"
    smoke_path.write_text(json.dumps(smoke_document), encoding="utf-8")
    smoke_runtime = load_runtime(smoke_path, environ=environment)
    assert smoke_runtime.connector.mapping.analysis_code == "analysis-node-smoke"
    with pytest.raises(ConnectorError, match="环境变量"):
        load_runtime(
            config_path,
            environ={**environment, "BIOWORKFLOW_WEBHOOK_SECRET": "invalid"},
        )


def test_connector_store_migrates_v1_state_without_losing_orders(tmp_path: Path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE connector_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO connector_metadata(key, value) VALUES('schema_version', '1');
            CREATE TABLE orders (
                external_run_id TEXT PRIMARY KEY,
                request_digest TEXT NOT NULL,
                run_id TEXT UNIQUE,
                status TEXT NOT NULL,
                status_version INTEGER NOT NULL,
                output_status TEXT,
                result_digest TEXT,
                export_id TEXT UNIQUE,
                export_manifest_digest TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    store = ConnectorStore(database)
    record, created = store.register_order("MES-MIGRATED-001", "sha256:" + "1" * 64)

    assert created is True
    assert record.result_manifest is None
    assert record.export_request is None
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM connector_metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(orders)").fetchall()
        }
    assert version == "4"
    assert {"result_manifest", "export_request_digest", "export_request"} <= columns


def test_urllib_transport_refuses_redirects():
    class RedirectHandler(BaseHTTPRequestHandler):
        target_hits = 0

        def log_message(self, _format, *_args):  # noqa: ANN001
            return

        def do_GET(self):  # noqa: ANN201, N802
            if self.path == "/start":
                port = self.server.server_address[1]
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            type(self).target_hits += 1
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = UrllibTransport().request(
            "GET",
            f"http://127.0.0.1:{server.server_address[1]}/start",
            headers={"Authorization": "Bearer must-not-be-forwarded"},
            body=None,
            timeout_seconds=2,
            max_response_bytes=1024,
        )
        assert response.status_code == 302
        assert RedirectHandler.target_hits == 0
        mes_client = MesConnectorClient(
            f"http://127.0.0.1:{server.server_address[1]}",
            "connector-token",
        )
        with pytest.raises(RuntimeError, match="HTTP 302"):
            mes_client.request("GET", "start")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    client = IntegrationClient(
        "https://analysis.example.com/api/v1/integration",
        "test-token",
    )
    with pytest.raises(ConnectorIntegrityError, match="不安全路径"):
        client._resolve_url(
            "/api/v1/integration/analysis-runs/%2e%2e/admin"
        )
    with pytest.raises(ValueError, match="HTTPS"):
        IntegrationClient(
            "http://analysis.example.com/api/v1/integration",
            "test-token",
        )
    with pytest.raises(ValueError, match="HTTPS"):
        MesConnectorClient("http://connector.example.com", "connector-token")


def test_loopback_http_clients_ignore_environment_proxy(monkeypatch):
    class DirectHandler(BaseHTTPRequestHandler):
        hits = 0

        def log_message(self, _format, *_args):  # noqa: ANN001
            return

        def do_GET(self):  # noqa: ANN201, N802
            type(self).hits += 1
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class ProxyHandler(BaseHTTPRequestHandler):
        hits = 0

        def log_message(self, _format, *_args):  # noqa: ANN001
            return

        def do_GET(self):  # noqa: ANN201, N802
            type(self).hits += 1
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    direct = HTTPServer(("127.0.0.1", 0), DirectHandler)
    proxy = HTTPServer(("127.0.0.1", 0), ProxyHandler)
    threads = [
        threading.Thread(target=direct.serve_forever, daemon=True),
        threading.Thread(target=proxy.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("http_proxy", proxy_url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    target_url = f"http://127.0.0.1:{direct.server_address[1]}"
    try:
        response = UrllibTransport().request(
            "GET",
            target_url,
            headers={"Authorization": "Bearer upstream-token"},
            body=None,
            timeout_seconds=2,
            max_response_bytes=1024,
        )
        assert response.status_code == 200
        assert MesConnectorClient(target_url, "inbound-token").request("GET", "") == {}
        assert DirectHandler.hits == 2
        assert ProxyHandler.hits == 0
    finally:
        direct.shutdown()
        proxy.shutdown()
        direct.server_close()
        proxy.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_duplicate_business_requests_create_one_analysis(simulated_connector):
    connector, transport = simulated_connector
    mes = SimulatedMESService(connector)

    first = mes.submit(_order())
    second = mes.submit(_order())

    assert first["id"] == second["id"]
    assert transport.openapi_calls == 2
    assert transport.submit_calls == 1
    assert transport.submission is not None
    assert "patient_name" not in json.dumps(transport.submission)

    changed = _order()
    changed["sample"]["code"] = "S002"
    with pytest.raises(ConnectorConflictError):
        mes.submit(changed)


@pytest.mark.parametrize(
    ("version", "has_request_digest"),
    [("1.4.0", True), ("1.5.0", False), ("2.0.0", True)],
)
def test_incompatible_integration_api_is_rejected_before_submit(
    simulated_connector,
    version: str,
    has_request_digest: bool,
):
    connector, transport = simulated_connector
    transport.openapi_version = version
    transport.openapi_has_request_digest = has_request_digest

    with pytest.raises(ConnectorError, match="1.5.0") as incompatible:
        connector.submit_order(_order("MES-INCOMPATIBLE-API-001"))

    assert incompatible.value.code == "CONNECTOR_INTEGRATION_API_INCOMPATIBLE"
    assert transport.submit_calls == 0
    assert transport.run is None


def test_submit_requires_a_pinned_product_contract_digest(simulated_connector):
    connector, transport = simulated_connector
    connector.mapping = _mapping(expected_digest="")

    with pytest.raises(ConnectorIntegrityError, match="expected_contract_digest"):
        connector.submit_order(_order("MES-UNPINNED-001"))

    assert transport.submit_calls == 0


def test_request_digest_matches_analysis_node_when_analysis_id_is_absent(
    simulated_connector,
):
    connector, _transport = simulated_connector
    order = _order("MES-NO-ANALYSIS-ID-001")
    order["order"].pop("analysis_id")

    run = connector.submit_order(order)

    assert run["request_digest"] == connector.store.get_order(
        "MES-NO-ANALYSIS-ID-001"
    ).request_digest


def test_submit_timeout_recovers_by_external_reference(simulated_connector):
    connector, transport = simulated_connector
    transport.fail_submit_after_commit = True

    recovered = connector.submit_order(_order("MES-TIMEOUT-001"))

    assert recovered["external_ref"]["external_run_id"] == "MES-TIMEOUT-001"
    assert transport.submit_calls == 1
    assert connector.order_status("MES-TIMEOUT-001")["run_id"] == recovered["id"]


def test_repeated_submit_recovers_unbound_run_before_mutable_product_checks(
    simulated_connector,
):
    connector, transport = simulated_connector
    transport.submit_response_failures = connector.client.recovery_attempts + 1
    transport.find_failures = connector.client.recovery_attempts + 1

    with pytest.raises(SubmissionUncertainError):
        connector.submit_order(_order("MES-CROSS-CALL-RECOVERY-001"))

    assert transport.run is not None
    assert connector.store.get_order("MES-CROSS-CALL-RECOVERY-001").run_id is None
    submit_calls = transport.submit_calls
    transport.product_ready = False

    recovered = connector.submit_order(_order("MES-CROSS-CALL-RECOVERY-001"))

    assert recovered["id"] == transport.run["id"]
    assert transport.submit_calls == submit_calls
    assert connector.store.get_order("MES-CROSS-CALL-RECOVERY-001").run_id == recovered["id"]


def test_submit_timeout_does_not_bind_a_conflicting_existing_run(simulated_connector):
    connector, transport = simulated_connector
    old_order = _order("MES-CONFLICT-TIMEOUT-001")
    old_order["sample"]["code"] = "OLD-SAMPLE"
    old_submission = _mapping().map_order(old_order).submission
    transport.submission = old_submission
    transport.run = transport._create_run(old_submission)
    transport.fail_conflict_response_once = True

    with pytest.raises(ConnectorIntegrityError, match="稳定标识"):
        connector.submit_order(_order("MES-CONFLICT-TIMEOUT-001"))

    assert connector.store.get_order("MES-CONFLICT-TIMEOUT-001").run_id is None


def test_reconcile_rejects_a_run_from_another_client(simulated_connector):
    connector, transport = simulated_connector
    connector.submit_order(_order())
    assert transport.run is not None
    transport.run["external_ref"]["client_id"] = "another-client"

    with pytest.raises(ConnectorIntegrityError, match="稳定标识"):
        connector.reconcile("MES-ORDER-001")


def test_webhook_replay_and_out_of_order_events_do_not_regress_state(
    simulated_connector,
):
    connector, transport = simulated_connector
    run = connector.submit_order(_order())
    transport.complete_run()
    latest = _terminal_event(run, version=4, status="succeeded")
    latest_headers, latest_body = _signed_event(latest)

    applied = connector.handle_webhook(latest_headers, latest_body)
    replayed = connector.handle_webhook(latest_headers, latest_body)
    stale_event = _terminal_event(run, version=3, status="failed")
    stale_headers, stale_body = _signed_event(stale_event)
    stale = connector.handle_webhook(stale_headers, stale_body)

    record = connector.store.get_order("MES-ORDER-001")
    assert applied.applied is True
    assert replayed.replayed is True
    assert stale.stale is True and stale.applied is False
    assert record.status == "succeeded"
    assert record.status_version == 4
    assert connector.store.event_count() == 2

    conflicting = {
        **latest,
        "event_id": str(uuid.uuid4()),
        "data": {**latest["data"], "output_status": "incomplete"},
    }
    conflicting_headers, conflicting_body = _signed_event(conflicting)
    with pytest.raises(ConnectorConflictError, match="输出状态"):
        connector.handle_webhook(conflicting_headers, conflicting_body)
    assert connector.store.get_order("MES-ORDER-001").output_status == "complete"
    assert connector.store.event_count() == 2

    invalid_output = {
        **latest,
        "event_id": str(uuid.uuid4()),
        "data": {**latest["data"], "output_status": "pending"},
    }
    invalid_headers, invalid_body = _signed_event(invalid_output)
    with pytest.raises(ConnectorError, match="output_status") as invalid:
        connector.handle_webhook(invalid_headers, invalid_body)
    assert invalid.value.code == "CONNECTOR_WEBHOOK_PAYLOAD_INVALID"
    assert connector.store.event_count() == 2

    tampered = {**latest, "data": {**latest["data"], "output_status": "incomplete"}}
    tampered["event_id"] = latest["event_id"]
    tampered_headers, tampered_body = _signed_event(tampered)
    with pytest.raises(ConnectorConflictError):
        connector.handle_webhook(tampered_headers, tampered_body)


def test_poll_allows_output_retention_change_without_a_new_status_version(
    simulated_connector,
):
    connector, transport = simulated_connector
    connector.submit_order(_order("MES-POLL-RETENTION-001"))
    assert transport.run is not None
    transport.run["output_status"] = "unavailable"

    connector.reconcile("MES-POLL-RETENTION-001")

    assert (
        connector.store.get_order("MES-POLL-RETENTION-001").output_status
        == "unavailable"
    )


def test_delayed_webhooks_cannot_bind_a_changed_lost_state_order(
    simulated_connector,
):
    connector, transport = simulated_connector
    external_run_id = "MES-LOST-STATE-001"
    old_order = _order(external_run_id)
    old_order["sample"]["code"] = "OLD-SAMPLE"
    old_submission = _mapping().map_order(old_order).submission
    transport.submission = old_submission
    transport.run = transport._create_run(old_submission)
    new_request = _mapping().map_order(_order(external_run_id))
    connector.store.register_order(external_run_id, new_request.request_digest)

    terminal = _terminal_event(transport.run, version=4, status="succeeded")
    headers, body = _signed_event(terminal)
    with pytest.raises(ConnectorIntegrityError, match="稳定标识"):
        connector.handle_webhook(headers, body)

    artifact = {
        "schema_version": "1.0.0",
        "event_id": str(uuid.uuid4()),
        "event_type": "analysis.artifact_export.completed",
        "occurred_at": "2026-08-30T00:00:00+00:00",
        "data": {
            "run_id": transport.run["id"],
            "external_ref": transport.run["external_ref"],
            "status_version": 4,
            "artifact_export_id": str(uuid.uuid4()),
            "manifest_digest": "sha256:" + "2" * 64,
        },
    }
    headers, body = _signed_event(artifact)
    with pytest.raises(ConnectorIntegrityError, match="稳定标识"):
        connector.handle_webhook(headers, body)

    assert connector.store.get_order(external_run_id).run_id is None


def test_result_digest_validation_and_mes_ingest_are_idempotent(simulated_connector):
    connector, transport = simulated_connector
    mes = SimulatedMESService(connector)
    mes.submit(_order())
    transport.complete_run()

    first = mes.ingest_results("MES-ORDER-001")
    second = mes.ingest_results("MES-ORDER-001")

    assert first["result_digest"] == second["result_digest"]
    assert mes.result_ingest_count == 1
    assert len(connector.store.list_outputs("MES-ORDER-001")) == 1
    assert Path(first["outputs"][0]["local_path"]).read_bytes() == transport.output_content
    assert first["manifest"]["results"][1] == {
        "key": "reference.summary",
        "name": "summary",
        "semantic_type": "report.qc_summary",
        "wdl_type": "Object",
        "required": False,
        "kind": "value",
        "value": {"sample_count": 1, "passed": True},
    }
    assert first["results"][1] == first["manifest"]["results"][1]

    reopened = ConnectorStore(connector.store.path)
    persisted = reopened.get_order("MES-ORDER-001")
    assert persisted.result_digest == first["result_digest"]
    assert persisted.result_manifest == first["manifest"]


def test_corrupt_download_is_rejected_before_result_ingest(simulated_connector):
    connector, transport = simulated_connector
    connector.submit_order(_order("MES-CORRUPT-001"))
    transport.complete_run(corrupt_download=True)

    with pytest.raises(ConnectorIntegrityError):
        connector.collect_results("MES-CORRUPT-001")

    assert connector.store.list_outputs("MES-CORRUPT-001") == []
    assert connector.store.get_order("MES-CORRUPT-001").result_digest is None


def test_missing_value_is_not_silently_converted_to_null(simulated_connector):
    connector, transport = simulated_connector
    connector.submit_order(_order("MES-MISSING-VALUE-001"))
    transport.complete_run()
    transport.omit_value = True

    with pytest.raises(ConnectorIntegrityError, match="缺少 value"):
        connector.collect_results("MES-MISSING-VALUE-001")

    record = connector.store.get_order("MES-MISSING-VALUE-001")
    assert record.result_digest is None
    assert record.result_manifest is None
    assert connector.store.list_outputs("MES-MISSING-VALUE-001") == []


def test_multi_file_results_are_not_exposed_until_the_whole_manifest_commits(
    simulated_connector,
):
    connector, transport = simulated_connector
    external_run_id = "MES-ATOMIC-RESULT-001"
    connector.submit_order(_order(external_run_id))
    transport.include_second_output = True
    transport.complete_run(corrupt_second_download=True)

    with pytest.raises(ConnectorIntegrityError, match="不一致"):
        connector.collect_results(external_run_id)

    status = connector.order_status(external_run_id)
    assert status["result_digest"] is None
    assert status["result_manifest"] is None
    assert status["outputs"] == []
    with pytest.raises(ConnectorError, match="尚未完整提交"):
        connector.open_output(external_run_id, "reference.result")

    inbound_token = "connector-inbound-token-with-32-chars"
    server = ReferenceConnectorHTTPServer(
        Runtime(
            connector=connector,
            server=ServerConfig("127.0.0.1", 0, 1024 * 1024),
            inbound_token=inbound_token,
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = Request(
        f"http://127.0.0.1:{server.server_address[1]}/v1/orders/"
        f"{external_run_id}/outputs/reference.result",
        headers={"Authorization": f"Bearer {inbound_token}"},
    )
    try:
        with pytest.raises(HTTPError) as unavailable:
            urlopen(request, timeout=2)
        assert unavailable.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_artifact_export_manifest_is_verified_and_acknowledged_once(
    simulated_connector,
):
    connector, transport = simulated_connector
    connector.submit_order(_order())
    transport.complete_run()
    export = connector.request_export("MES-ORDER-001", profile="mes-results")
    transport.complete_export()

    first = connector.complete_export("MES-ORDER-001")
    second = connector.complete_export("MES-ORDER-001")

    assert first["id"] == export["id"] == second["id"]
    assert transport.ack_count == 1
    assert first["acknowledged_at"]


def test_artifact_export_request_is_claimed_before_upstream_side_effect(
    simulated_connector,
):
    connector, transport = simulated_connector
    connector.submit_order(_order())
    barrier = threading.Barrier(3)
    outcomes: list[object] = []

    def request(profile: str) -> None:
        barrier.wait()
        try:
            outcomes.append(
                connector.request_export("MES-ORDER-001", profile=profile)
            )
        except ConnectorError as error:
            outcomes.append(error)

    workers = [
        threading.Thread(target=request, args=(profile,))
        for profile in ("mes-results-a", "mes-results-b")
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert transport.export_submit_calls == 1
    assert sum(isinstance(item, ConnectorConflictError) for item in outcomes) == 1


def test_artifact_export_rejects_consistent_but_wrong_manifest_identity(
    simulated_connector,
):
    connector, transport = simulated_connector
    connector.submit_order(_order())
    transport.complete_run()
    connector.request_export("MES-ORDER-001", profile="mes-results")
    transport.complete_export()
    assert transport.export is not None
    transport.export["manifest"]["run_id"] = str(uuid.uuid4())
    transport.export["manifest_digest"] = canonical_digest(
        transport.export["manifest"]
    )

    with pytest.raises(ConnectorIntegrityError, match="身份"):
        connector.complete_export("MES-ORDER-001")


@pytest.mark.parametrize("malformation", ["missing_name", "missing_destination", "missing_value"])
def test_artifact_export_rejects_incomplete_manifest_before_ack(
    simulated_connector,
    malformation: str,
):
    connector, transport = simulated_connector
    connector.submit_order(_order())
    connector.request_export("MES-ORDER-001", profile="mes-results")
    transport.complete_export()
    assert transport.export is not None
    manifest = transport.export["manifest"]
    if malformation == "missing_name":
        manifest["items"][0].pop("name")
    elif malformation == "missing_destination":
        manifest["items"][0].pop("destination")
    else:
        manifest["items"].append(
            {
                "key": "reference.summary",
                "name": "summary",
                "semantic_type": "report.qc_summary",
                "wdl_type": "Object",
                "required": False,
                "kind": "value",
            }
        )
        manifest["summary"]["item_count"] = 2
    transport.export["manifest_digest"] = canonical_digest(manifest)

    with pytest.raises(ConnectorIntegrityError):
        connector.complete_export("MES-ORDER-001")

    assert transport.ack_count == 0


def test_artifact_export_rejects_a_destination_outside_the_fixed_target(
    simulated_connector,
):
    connector, transport = simulated_connector
    connector.submit_order(_order())
    connector.request_export("MES-ORDER-001", profile="mes-results")
    transport.complete_export()
    assert transport.export is not None
    item = transport.export["manifest"]["items"][0]
    item["destination"] = {
        "type": "s3_object",
        "profile": "other-profile",
        "bucket": "other-bucket",
        "key": f"{transport.export['run_id']}/{transport.export['id']}/result.tsv",
        "uri": "s3://other-bucket/result.tsv",
        "size": item["size"],
        "sha256": item["sha256"],
    }
    transport.export["manifest_digest"] = canonical_digest(
        transport.export["manifest"]
    )

    with pytest.raises(ConnectorIntegrityError, match="目标"):
        connector.complete_export("MES-ORDER-001")

    assert transport.ack_count == 0


def test_artifact_webhook_replay_is_idempotent(simulated_connector):
    connector, transport = simulated_connector
    run = connector.submit_order(_order())
    export = connector.request_export("MES-ORDER-001", profile="mes-results")
    transport.complete_export()
    assert transport.export is not None
    event = {
        "schema_version": "1.0.0",
        "event_id": str(uuid.uuid4()),
        "event_type": "analysis.artifact_export.completed",
        "occurred_at": "2026-08-30T00:00:00+00:00",
        "data": {
            "run_id": run["id"],
            "external_ref": run["external_ref"],
            "status_version": 4,
            "artifact_export_id": export["id"],
            "manifest_digest": transport.export["manifest_digest"],
        },
    }
    headers, body = _signed_event(event)

    applied = connector.handle_webhook(headers, body)
    transport.fail_export_lookup = True
    replayed = connector.handle_webhook(headers, body)

    record = connector.store.get_order("MES-ORDER-001")
    assert applied.applied is True
    assert replayed.replayed is True
    assert transport.export_lookup_calls == 1
    assert record.export_manifest_digest == transport.export["manifest_digest"]

    changed = {
        **event,
        "data": {**event["data"], "manifest_digest": "sha256:" + "f" * 64},
    }
    changed_headers, changed_body = _signed_event(changed)
    with pytest.raises(ConnectorConflictError, match="不同 payload"):
        connector.handle_webhook(changed_headers, changed_body)
    assert transport.export_lookup_calls == 1


def test_artifact_webhook_rejects_a_digest_not_confirmed_by_the_export(
    simulated_connector,
):
    connector, transport = simulated_connector
    run = connector.submit_order(_order("MES-EXPORT-DIGEST-001"))
    export = connector.request_export(
        "MES-EXPORT-DIGEST-001",
        profile="mes-results",
    )
    transport.complete_export()
    assert transport.export is not None
    actual_digest = transport.export["manifest_digest"]
    event = {
        "schema_version": "1.0.0",
        "event_id": str(uuid.uuid4()),
        "event_type": "analysis.artifact_export.completed",
        "occurred_at": "2026-08-30T00:00:00+00:00",
        "data": {
            "run_id": run["id"],
            "external_ref": run["external_ref"],
            "status_version": 4,
            "artifact_export_id": export["id"],
            "manifest_digest": "sha256:" + "f" * 64,
        },
    }
    headers, body = _signed_event(event)

    with pytest.raises(ConnectorIntegrityError, match="权威 export"):
        connector.handle_webhook(headers, body)

    assert connector.store.get_order("MES-EXPORT-DIGEST-001").export_manifest_digest is None
    assert connector.store.event_count() == 0

    event["data"]["manifest_digest"] = actual_digest
    headers, body = _signed_event(event)
    accepted = connector.handle_webhook(headers, body)
    assert accepted.applied is True
    assert connector.complete_export("MES-EXPORT-DIGEST-001")["acknowledged_at"]


def test_artifact_webhook_rejects_an_export_that_is_not_succeeded(
    simulated_connector,
):
    connector, transport = simulated_connector
    run = connector.submit_order(_order("MES-EXPORT-PENDING-001"))
    export = connector.request_export(
        "MES-EXPORT-PENDING-001",
        profile="mes-results",
    )
    event = {
        "schema_version": "1.0.0",
        "event_id": str(uuid.uuid4()),
        "event_type": "analysis.artifact_export.completed",
        "occurred_at": "2026-08-30T00:00:00+00:00",
        "data": {
            "run_id": run["id"],
            "external_ref": run["external_ref"],
            "status_version": 1,
            "artifact_export_id": export["id"],
            "manifest_digest": "sha256:" + "e" * 64,
        },
    }
    headers, body = _signed_event(event)

    with pytest.raises(ConnectorIntegrityError, match="权威 export"):
        connector.handle_webhook(headers, body)

    assert connector.store.get_order("MES-EXPORT-PENDING-001").export_manifest_digest is None
    assert connector.store.event_count() == 0


def test_reference_connector_http_boundary_requires_inbound_token(
    simulated_connector,
):
    connector, transport = simulated_connector
    inbound_token = "connector-inbound-token-with-32-chars"
    runtime = Runtime(
        connector=connector,
        server=ServerConfig("127.0.0.1", 0, 1024 * 1024),
        inbound_token=inbound_token,
    )
    server = ReferenceConnectorHTTPServer(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base_url}/health", timeout=2) as response:
            assert json.load(response) == {"status": "ok"}
        with pytest.raises(HTTPError) as unauthorized:
            urlopen(f"{base_url}/v1/products", timeout=2)
        assert unauthorized.value.code == 401
        pipelined = (
            "GET /health HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.server_address[1]}\r\n\r\n"
        ).encode()
        with socket.create_connection(server.server_address, timeout=2) as connection:
            connection.sendall(
                (
                    "POST /v1/orders HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{server.server_address[1]}\r\n"
                    f"Content-Length: {len(pipelined)}\r\n"
                    "Connection: keep-alive\r\n\r\n"
                ).encode()
                + pipelined
            )
            response = b""
            while chunk := connection.recv(4096):
                response += chunk
        assert response.count(b"HTTP/1.1") == 1
        assert b" 401 " in response
        assert b"Connection: close" in response
        request = Request(
            f"{base_url}/v1/products",
            headers={"Authorization": f"Bearer {inbound_token}"},
        )
        with urlopen(request, timeout=2) as response:
            assert len(json.load(response)["results"]) == 1
        connector.mapping = MappingConfig(
            **{
                **_mapping().__dict__,
                "input_mappings": {
                    "probe": {"path": "files.probe", "kind": "reference"}
                },
            }
        )
        invalid_order = _order("MES-INVALID-REFERENCE-001")
        invalid_order["files"]["probe"] = {
            "root_alias": "rawdata",
            "relative_path": "../secret.txt",
        }
        invalid_request = Request(
            f"{base_url}/v1/orders",
            data=json.dumps(invalid_order).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {inbound_token}",
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(HTTPError) as invalid_reference:
            urlopen(invalid_request, timeout=2)
        assert invalid_reference.value.code == 400
        error_envelope = json.load(invalid_reference.value)
        assert error_envelope["error"]["details"]["path"] == "inputs.probe"
        transport.redirect_products = True
        with pytest.raises(HTTPError) as redirected:
            urlopen(request, timeout=2)
        assert redirected.value.code == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_reference_connector_error_envelopes_are_stable(
    simulated_connector,
    monkeypatch,
):
    connector, _transport = simulated_connector
    inbound_token = "connector-inbound-token-with-32-chars"
    server = ReferenceConnectorHTTPServer(
        Runtime(
            connector=connector,
            server=ServerConfig("127.0.0.1", 0, 1024 * 1024),
            inbound_token=inbound_token,
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def authenticated_request(path: str) -> Request:
        return Request(
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {inbound_token}"},
        )

    try:
        with pytest.raises(HTTPError) as missing_route:
            urlopen(authenticated_request("/v1/missing"), timeout=2)
        assert missing_route.value.code == 404
        missing_error = json.load(missing_route.value)["error"]
        assert set(missing_error) == {"code", "message", "retryable", "details"}
        assert missing_error["code"] == "CONNECTOR_ROUTE_NOT_FOUND"

        unsupported = authenticated_request("/v1/products")
        unsupported.method = "PUT"
        with pytest.raises(HTTPError) as unsupported_method:
            urlopen(unsupported, timeout=2)
        assert unsupported_method.value.code == 501
        method_error = json.load(unsupported_method.value)["error"]
        assert set(method_error) == {"code", "message", "retryable", "details"}
        assert method_error["code"] == "CONNECTOR_HTTP_ERROR"

        def fail_unexpectedly():
            raise RuntimeError("must not escape the error boundary")

        monkeypatch.setattr(connector.client, "list_products", fail_unexpectedly)
        with pytest.raises(HTTPError) as internal_error:
            urlopen(authenticated_request("/v1/products"), timeout=2)
        assert internal_error.value.code == 500
        internal = json.load(internal_error.value)["error"]
        assert set(internal) == {"code", "message", "retryable", "details"}
        assert internal["code"] == "CONNECTOR_INTERNAL_ERROR"
        assert internal["details"] == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_reference_connector_serves_only_authenticated_verified_outputs(
    simulated_connector,
    tmp_path: Path,
):
    connector, transport = simulated_connector
    connector.submit_order(_order())
    transport.complete_run()
    collected = connector.collect_results("MES-ORDER-001")
    output = collected["outputs"][0]
    inbound_token = "connector-inbound-token-with-32-chars"
    runtime = Runtime(
        connector=connector,
        server=ServerConfig("127.0.0.1", 0, 1024 * 1024),
        inbound_token=inbound_token,
    )
    server = ReferenceConnectorHTTPServer(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}{output['download_url']}"
    try:
        with pytest.raises(HTTPError) as unauthorized:
            urlopen(url, timeout=2)
        assert unauthorized.value.code == 401
        request = Request(
            url,
            headers={"Authorization": f"Bearer {inbound_token}"},
        )
        with urlopen(request, timeout=2) as response:
            assert response.read() == transport.output_content
            assert response.headers["X-Checksum-SHA256"] == output["sha256"]
        client = MesConnectorClient(
            f"http://127.0.0.1:{server.server_address[1]}",
            inbound_token,
        )
        client_result = client.download(
            "MES-ORDER-001",
            "reference.result",
            str(tmp_path / "mes-result.tsv"),
        )
        assert Path(client_result["path"]).read_bytes() == transport.output_content
        assert client_result["sha256"] == output["sha256"]

        Path(output["local_path"]).write_bytes(b"tampered")
        with pytest.raises(HTTPError) as corrupted:
            urlopen(request, timeout=2)
        assert corrupted.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class DjangoTransport:
    def __init__(self, raw_token: str) -> None:
        self.client = APIClient()
        self.raw_token = raw_token

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del timeout_seconds
        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        extra = {
            "HTTP_" + key.upper().replace("-", "_"): value
            for key, value in headers.items()
            if key.casefold() != "content-type"
        }
        assert extra["HTTP_AUTHORIZATION"] == f"Bearer {self.raw_token}"
        response = self.client.generic(
            method,
            path,
            data=body or b"",
            content_type=headers.get("Content-Type", "application/octet-stream"),
            **extra,
        )
        content = (
            b"".join(response.streaming_content)
            if getattr(response, "streaming", False)
            else response.content
        )
        if len(content) > max_response_bytes:
            raise IntegrationTransportError("Django test response exceeded limit")
        return TransportResponse(
            response.status_code,
            dict(response.items()),
            content,
        )


@pytest.mark.django_db
def test_reference_connector_completes_real_integration_api_loop(
    settings,
    tmp_path: Path,
):
    rawdata = tmp_path / "rawdata"
    runs = tmp_path / "runs"
    rawdata.mkdir()
    runs.mkdir()
    (rawdata / "incoming").mkdir()
    probe = rawdata / "incoming" / "S001.txt"
    probe.write_text("analysis-node-input\n", encoding="utf-8")
    settings.ANALYSIS_RAWDATA_ROOT = rawdata
    settings.ANALYSIS_RAWDATA_EXECUTION_ROOT = rawdata
    settings.ANALYSIS_RUN_ROOT = runs
    settings.ANALYSIS_RUN_EXECUTION_ROOT = runs
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 0
    settings.INTEGRATION_REQUIRE_ANALYSIS_PRODUCT = True
    settings.INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE = True
    call_command("prepare_analysis_node_smoke", actor="pytest")
    product = AnalysisProductVersion.objects.select_related("product").get(
        product__code="analysis-node-smoke",
        contract_version="1.0.0",
    )
    account = ServiceAccount.objects.create(
        client_id="mes-reference",
        name="Reference MES",
        scopes=[
            "workflow:read",
            "analysis:submit",
            "analysis:read",
            "analysis:download",
        ],
    )
    _, raw_token = issue_service_token(account, name="connector", actor="pytest")
    connector = ReferenceConnector(
        mapping=_mapping(
            analysis_code="analysis-node-smoke",
            expected_digest=product.contract_digest,
        ),
        client=IntegrationClient(
            "http://127.0.0.1/api/v1/integration",
            raw_token,
            transport=DjangoTransport(raw_token),
        ),
        store=ConnectorStore(tmp_path / "connector-state" / "state.sqlite3"),
        webhook_secret=WEBHOOK_SECRET,
        result_directory=tmp_path / "connector-results",
    )

    submitted = connector.submit_order(_order("MES-REAL-001"))
    run = AnalysisRun.objects.get(pk=submitted["id"])
    run_directory = runs / str(run.id)
    run_directory.mkdir()
    result_file = run_directory / "result.txt"
    result_file.write_text("analysis-node-input\n", encoding="utf-8")
    output_key = run.request_payload["integration_output_contract"][0]["key"]
    run.work_directory = str(run_directory)
    run.outputs = {"outputs": {output_key: str(result_file)}}
    manifest, output_status, error = build_output_manifest(run, run.outputs)
    assert error is None
    run.output_manifest = manifest
    run.output_status = output_status
    run.status = AnalysisRun.Status.SUCCEEDED
    run.status_version = 4
    run.progress = 100
    run.finished_at = timezone.now()
    run.save(
        update_fields=[
            "work_directory",
            "outputs",
            "output_manifest",
            "output_status",
            "status",
            "status_version",
            "progress",
            "finished_at",
            "updated_at",
        ]
    )
    submitted.update(
        {
            "status": "succeeded",
            "status_version": 4,
            "output_status": "complete",
            "external_ref": run.request_payload["external_ref"],
            "analysis_product": run.request_payload["analysis_product"],
        }
    )
    event = _terminal_event(submitted, version=4, status="succeeded")
    headers, body = _signed_event(event)

    webhook = connector.handle_webhook(headers, body)
    collected = connector.collect_results("MES-REAL-001")

    assert webhook.applied is True
    assert collected["outputs"][0]["sha256"].startswith("sha256:")
    assert Path(collected["outputs"][0]["local_path"]).read_bytes() == probe.read_bytes()

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from workflows import data_service
from workflows.models import RawdataDatasetIndex, RawdataScan
from workflows.rawdata_readiness import verify_readiness_snapshot
from test_integration_api import _token_client
from test_rawdata_readiness import validate_response as validate_response

pytestmark = pytest.mark.django_db


@pytest.fixture
def remote(settings):
    settings.DATA_SERVICE_URL = "http://data-api:8000"
    return settings


def test_existing_scope_and_legacy_representation(remote, validate_response):
    _, _, _, client = _token_client(scopes=["analysis:read"])
    response = {"count": 0, "truncated": False, "results": []}
    with patch("workflows.data_service.request", return_value=response) as upstream:
        result = client.get("/api/v1/integration/rawdata-datasets")
    assert result.status_code == 200
    validate_response(result, "/rawdata-datasets")
    upstream.assert_called_once_with("GET", "/api/v1/datasets")
    assert RawdataDatasetIndex.objects.count() == 0
    _, _, _, denied = _token_client(scopes=["workflow:read"], client_id="no-read")
    with patch("workflows.data_service.request") as upstream:
        assert denied.get("/api/v1/integration/rawdata-datasets").status_code == 403
        upstream.assert_not_called()


def test_outage_and_invalid_snapshot_fail_closed(remote, validate_response):
    _, _, _, client = _token_client(scopes=["analysis:read"])
    with patch(
        "workflows.data_service.request",
        side_effect=data_service.DataServiceUnavailable(),
    ):
        response = client.get("/api/v1/integration/rawdata-datasets")
        assert response.status_code == 503
        assert response.data["error"]["retryable"] is True
        assert response.data["error"]["code"] == "DATA_SERVICE_UNAVAILABLE"
    with patch("workflows.data_service.request", return_value={"snapshot": {}}):
        with pytest.raises(data_service.DataServiceUnavailable):
            verify_readiness_snapshot({"schema_version": 1}, {})


def test_worker_verifies_remote_snapshot_and_local_manifest(remote, monkeypatch):
    from types import SimpleNamespace
    from workflows.analysis_runtime import _verify_run_resource_manifests

    snapshot = {"schema_version": 1, "files": ["one"]}
    manifest = {"files": [{"relative_path": "one"}]}
    with (
        patch(
            "workflows.data_service.request", return_value={"snapshot": snapshot}
        ) as upstream,
        patch("workflows.analysis_runtime._verify_manifest_files") as local,
    ):
        _verify_run_resource_manifests(
            SimpleNamespace(
                request_payload={
                    "rawdata_readiness": snapshot,
                    "input_resource_manifest": manifest,
                }
            )
        )
        upstream.assert_called_once_with(
            "POST",
            "/api/v1/verify",
            {"snapshot": snapshot, "manifest": manifest, "require_fresh_index": False},
        )
        assert local.called


def test_legacy_indexer_cannot_write_after_switch(remote):
    with pytest.raises(CommandError, match="Independent data service"):
        call_command("run_rawdata_indexer", once=True)
    assert not RawdataScan.objects.exists()


def test_admin_scan_and_catalog_are_forwarded(remote, settings):
    settings.AUTH_REQUIRED = False
    client = APIClient()
    with patch(
        "workflows.data_service.request", return_value={"created": True, "id": "scan"}
    ):
        assert (
            client.post("/api/v1/rawdata/scans", {}, format="json").status_code == 202
        )
    with patch(
        "workflows.data_service.catalog", return_value={"datasets": [], "summary": {}}
    ):
        assert client.get("/api/v1/rawdata/catalog").status_code == 200


@pytest.mark.parametrize(
    "code,path,exception",
    [
        ("RAWDATA_NOT_READY", "/api/v1/verify", ValueError),
        (
            "RAWDATA_REQUEST_INVALID",
            "/api/v1/verify",
            data_service.DataServiceUnavailable,
        ),
        ("RAWDATA_REQUEST_INVALID", "/api/v1/readiness", ValueError),
        ("BROKEN", "/api/v1/verify", data_service.DataServiceUnavailable),
    ],
)
def test_remote_bad_request_classification(remote, tmp_path, code, path, exception):
    import json
    from io import BytesIO
    from urllib.error import HTTPError

    token = tmp_path / "token"
    token.write_text("synthetic-test-token")
    remote.DATA_SERVICE_TOKEN_FILE = str(token)
    response = HTTPError(
        "http://data-api",
        400,
        "invalid",
        {},
        BytesIO(json.dumps({"error": {"code": code}}).encode()),
    )
    with patch("workflows.data_service.build_opener") as opener:
        opener.return_value.open.side_effect = response
        with pytest.raises(exception):
            data_service.request("POST", path, {})


def test_non_json_error_is_infrastructure_failure(remote, tmp_path):
    from io import BytesIO
    from urllib.error import HTTPError

    token = tmp_path / "token"
    token.write_text("synthetic-test-token")
    remote.DATA_SERVICE_TOKEN_FILE = str(token)
    with patch("workflows.data_service.build_opener") as opener:
        opener.return_value.open.side_effect = HTTPError(
            "http://data-api", 400, "invalid", {}, BytesIO(b"not-json")
        )
        with pytest.raises(data_service.DataServiceUnavailable):
            data_service.request("POST", "/api/v1/verify", {})


def test_worker_outage_retains_infrastructure_retry_metadata():
    from types import SimpleNamespace
    from workflows.analysis_runtime import process_analysis_run

    run = SimpleNamespace(pk="synthetic", lease_token=None)
    with (
        patch("workflows.analysis_runtime._LeaseHeartbeat"),
        patch(
            "workflows.analysis_runtime.execute_analysis_run",
            side_effect=data_service.DataServiceUnavailable(),
        ),
        patch("workflows.analysis_runtime._update_run") as update,
        patch("workflows.analysis_runtime._event"),
    ):
        process_analysis_run(run)
    assert update.call_args.kwargs["error_code"] == "DATA_SERVICE_UNAVAILABLE"
    assert update.call_args.kwargs["error_category"] == "infrastructure"
    assert update.call_args.kwargs["error_retryable"] is True
    assert update.call_args.kwargs["status"] == "failed"

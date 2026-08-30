from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from rest_framework.test import APIClient

from workflows import api_overrides, views, wdl_assets
from workflows.models import ToolDocument, ToolVersion, WorkflowDocument, WorkflowVersion
from workflows.request_ids import request_id
import workflows.urls as workflow_urls


pytestmark = pytest.mark.usefixtures("auth_disabled")


ROOT = Path(__file__).resolve().parents[2]


def test_request_id_accepts_standard_uuid_header():
    value = "550e8400-e29b-41d4-a716-446655440000"
    request = SimpleNamespace(headers={"X-Request-ID": value})

    assert request_id(request) == value


def test_request_id_replaces_invalid_header():
    request = SimpleNamespace(headers={"X-Request-ID": "bad request id"})

    generated = request_id(request)

    assert generated.startswith("req_")
    assert generated != "bad request id"


def test_request_id_helpers_are_shared_by_api_modules():
    assert workflow_urls.urlpatterns
    assert views._request_id is request_id
    assert wdl_assets._request_id is request_id


def test_health_endpoint_echoes_valid_request_id():
    value = "550e8400-e29b-41d4-a716-446655440000"

    response = APIClient().get("/api/v1/health", HTTP_X_REQUEST_ID=value)

    assert response.status_code == 200
    assert response["X-Request-ID"] == value


def test_ready_endpoint_returns_503_when_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(views, "_database_status", lambda: "unavailable")

    response = APIClient().get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["dependencies"]["database"] == "unavailable"


def test_health_endpoint_does_not_probe_database(monkeypatch):
    def unexpected_database_probe():
        raise AssertionError("liveness must not access the database")

    monkeypatch.setattr(views, "_database_status", unexpected_database_probe)

    response = APIClient().get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["dependencies"]["database"] == "not_checked"


@pytest.mark.django_db
def test_workflow_listing_is_bounded_and_uses_annotated_latest_version():
    for slug in ("zeta", "alpha", "beta"):
        WorkflowDocument.objects.create(
            slug=slug,
            name=slug.title(),
            kind=(
                WorkflowDocument.Kind.SUBWORKFLOW
                if slug == "alpha"
                else WorkflowDocument.Kind.WORKFLOW
            ),
        )
    alpha = WorkflowDocument.objects.get(slug="alpha")
    WorkflowVersion.objects.create(
        workflow=alpha,
        version=1,
        name="Alpha 1",
        semantic_digest="sha256:alpha-1",
        workflow_graph={},
        kind=WorkflowDocument.Kind.SUBWORKFLOW,
    )
    WorkflowDocument.objects.bulk_create(
        [
            WorkflowDocument(slug=f"zz-{index:03d}", name=f"Workflow {index}")
            for index in range(98)
        ]
    )
    WorkflowVersion.objects.create(
        workflow=alpha,
        version=3,
        name="Alpha 3",
        semantic_digest="sha256:alpha-3",
        workflow_graph={},
        kind=WorkflowDocument.Kind.SUBWORKFLOW,
    )

    with CaptureQueriesContext(connection) as captured:
        response = APIClient().get(
            "/api/v1/editor/workflows?page=1&page_size=2"
        )

    assert response.status_code == 200
    assert len(captured) <= 4
    assert [item["slug"] for item in response.json()["results"]] == [
        "alpha",
        "beta",
    ]
    assert response.json()["results"][0]["latest_version"] == 3
    assert response.json()["results"][0]["latest_version_snapshot"]["version"] == 3
    assert response.json()["total"] == 101
    assert response.json()["has_next"] is True

    next_page = APIClient().get(
        "/api/v1/editor/workflows?page=2&page_size=2"
    )
    assert [item["slug"] for item in next_page.json()["results"]] == [
        "zeta",
        "zz-000",
    ]
    assert next_page.json()["has_previous"] is True

    default_page = APIClient().get("/api/v1/editor/workflows")
    assert [item["slug"] for item in default_page.json()["results"][:3]] == [
        "alpha",
        "beta",
        "zeta",
    ]
    assert len(default_page.json()["results"]) == 50
    assert default_page.json()["page_size"] == 50
    assert default_page.json()["has_next"] is True

    clamped = APIClient().get(
        "/api/v1/editor/workflows?page=1&page_size=1000"
    )
    assert len(clamped.json()["results"]) == 100
    assert clamped.json()["page_size"] == 100
    assert clamped.json()["has_next"] is True

    invalid = APIClient().get(
        "/api/v1/editor/workflows?page=1000001"
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "WORKFLOW_PAGE_INVALID"


def test_default_host_and_cors_configuration_is_restricted():
    assert "*" not in settings.ALLOWED_HOSTS
    assert "localhost" in settings.ALLOWED_HOSTS
    assert "127.0.0.1" in settings.ALLOWED_HOSTS
    cors_index = settings.MIDDLEWARE.index("config.cors.CorsMiddleware")
    common_index = settings.MIDDLEWARE.index("django.middleware.common.CommonMiddleware")
    assert cors_index < common_index
    assert settings.CORS_ALLOWED_ORIGINS == [
        "https://wdl.qzhqzh.com",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_cors_preflight_allows_configured_origin():
    response = APIClient().options(
        "/api/v1/health",
        HTTP_ORIGIN="http://localhost:3000",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="X-Request-ID",
    )

    assert response.status_code == 204
    assert response["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "GET" in response["Access-Control-Allow-Methods"]
    assert "X-Request-ID" in response["Access-Control-Allow-Headers"]


@pytest.mark.django_db
def test_tools_endpoint_uses_bounded_queries():
    ToolVersion.objects.create(
        tool_id="alpha",
        version="1.0.0",
        name="Alpha old",
        digest="sha256:old",
        tool_spec={"id": "alpha"},
    )
    ToolVersion.objects.create(
        tool_id="alpha",
        version="1.1.0",
        name="Alpha latest",
        digest="sha256:new",
        tool_spec={"id": "alpha"},
    )
    ToolVersion.objects.create(
        tool_id="beta",
        version="2.0.0",
        name="Beta",
        digest="sha256:beta",
        tool_spec={"id": "beta"},
    )
    ToolDocument.objects.create(
        tool_id="alpha",
        draft_spec={"id": "alpha", "display_name": "Alpha draft"},
        validation={"status": "valid"},
    )

    assert resolve("/api/v1/tools").func is api_overrides.tools
    with CaptureQueriesContext(connection) as captured:
        response = APIClient().get("/api/v1/tools")

    assert response.status_code == 200
    assert len(captured) <= 3
    results = {item["tool_id"]: item for item in response.json()["results"]}
    assert results["alpha"]["name"] == "Alpha draft"
    assert results["alpha"]["latest_version"] == "1.1.0"
    assert results["alpha"]["version_count"] == 2
    assert results["beta"]["version_count"] == 1


def test_container_and_entrypoint_regressions():
    frontend = (ROOT / "Dockerfile.frontend").read_text(encoding="utf-8")
    backend = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "backend-entrypoint.sh").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    nginx = (ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")

    assert "ARG BUN_BASE_IMAGE=" in frontend
    assert "FROM ${BUN_BASE_IMAGE} AS build" in frontend
    assert "apt-get install -y --no-install-recommends musl" not in backend
    assert 'if [ "${DJANGO_SEED_DEMO:-0}" = "1" ]; then' in entrypoint
    assert 'DJANGO_ALLOWED_HOSTS: "*"' not in compose
    assert 'DJANGO_SEED_DEMO: "${DJANGO_SEED_DEMO:-0}"' in compose
    assert "resolver 127.0.0.11" in nginx
    assert "proxy_pass http://$backend_upstream" in nginx
    assert "proxy_pass http://$frontend_upstream" in nginx

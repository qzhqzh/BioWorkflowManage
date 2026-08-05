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
from workflows.models import ToolDocument, ToolVersion
from workflows.request_ids import request_id
import workflows.urls as workflow_urls


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

    assert frontend.startswith("FROM oven/bun:1.3.14 AS build")
    assert "apt-get install -y --no-install-recommends musl" not in backend
    assert 'if [ "${DJANGO_SEED_DEMO:-0}" = "1" ]; then' in entrypoint
    assert 'DJANGO_ALLOWED_HOSTS: "*"' not in compose
    assert 'DJANGO_SEED_DEMO: "${DJANGO_SEED_DEMO:-0}"' in compose

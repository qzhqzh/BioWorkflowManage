from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from io import StringIO
from pathlib import Path

import pytest
import WDL
from django.core.management import call_command
from django.core.management.base import CommandError

from workflows.management.commands.prepare_analysis_node_smoke import (
    smoke_workflow_snapshot,
)
from workflows.models import AnalysisProductVersion, WorkflowDocument, WorkflowVersion


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = PROJECT_ROOT / "deploy" / "analysis-node" / "lib" / "analysis_node.py"
SPEC = importlib.util.spec_from_file_location("analysis_node_cli", CLI_PATH)
assert SPEC is not None and SPEC.loader is not None
analysis_node = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis_node
SPEC.loader.exec_module(analysis_node)

BUILDER_PATH = PROJECT_ROOT / "scripts" / "build_analysis_node_bundle.py"
BUILDER_SPEC = importlib.util.spec_from_file_location(
    "analysis_node_bundle_builder",
    BUILDER_PATH,
)
assert BUILDER_SPEC is not None and BUILDER_SPEC.loader is not None
bundle_builder = importlib.util.module_from_spec(BUILDER_SPEC)
sys.modules[BUILDER_SPEC.name] = bundle_builder
BUILDER_SPEC.loader.exec_module(bundle_builder)


def _config(tmp_path: Path, *, runtime: str = "isolated") -> dict[str, str]:
    data = tmp_path / "data"
    workspace = data / "workspace"
    host_paths = {
        "ANALYSIS_RAWDATA_HOST_PATH": workspace / "rawdata",
        "ANALYSIS_DATABASE_HOST_PATH": workspace / "databases",
        "ANALYSIS_RUN_HOST_PATH": workspace / "runs",
        "ANALYSIS_INPUT_STAGING_HOST_PATH": workspace / "staging",
    }
    values = {
        "ANALYSIS_NODE_VERSION": "1.2.3",
        "ANALYSIS_NODE_MODE": "headless",
        "ANALYSIS_NODE_RUNTIME": runtime,
        "ANALYSIS_NODE_BIND_ADDRESS": "127.0.0.1",
        "ANALYSIS_NODE_API_PORT": "18082",
        "ANALYSIS_NODE_CONSOLE_PORT": "18083",
        "ANALYSIS_NODE_PUBLIC_BASE_URL": "https://analysis.example.com",
        "POSTGRES_DB": "bioworkflow",
        "POSTGRES_USER": "bioworkflow",
        "POSTGRES_PASSWORD": "database-secret-123456",
        "DJANGO_SECRET_KEY": "django-secret-key-with-at-least-32-characters",
        "WEBHOOK_SIGNING_KEY": "webhook-secret-key-with-at-least-32-characters",
        "INTEGRATION_REQUIRE_ANALYSIS_PRODUCT": "1",
        "ANALYSIS_NODE_DATA_ROOT": str(data),
        "ANALYSIS_NODE_BACKUP_ROOT": str(tmp_path / "backups"),
        "ANALYSIS_NODE_POSTGRES_PATH": str(data / "postgres"),
        "ANALYSIS_NODE_DIND_PATH": str(data / "dind"),
        "ANALYSIS_NODE_DIND_CERT_PATH": str(data / "dind-certs"),
        "ANALYSIS_WORKSPACE_HOST_PATH": str(workspace),
        **{key: str(value) for key, value in host_paths.items()},
        "ANALYSIS_CACHE_HOST_PATH": str(workspace / "cache"),
        "ANALYSIS_ARTIFACT_EXPORT_HOST_PATH": str(workspace / "exports"),
        "ANALYSIS_OBJECT_STORAGE_SECRETS_HOST_PATH": str(tmp_path / "secrets" / "objects"),
        "ANALYSIS_ARTIFACT_EXPORT_SECRETS_HOST_PATH": str(tmp_path / "secrets" / "exports"),
        "MINIWDL_CONTROL_SUBNET": "10.253.0.0/24",
        "MINIWDL_EGRESS_SUBNET": "10.253.1.0/24",
        "MINIWDL_UID": str(os.geteuid()),
        "MINIWDL_GID": str(os.getegid()),
        "MINIWDL_DOCKER_GID": "998",
    }
    isolated = {
        "ANALYSIS_RAWDATA_EXECUTION_ROOT": "/analysis/rawdata",
        "ANALYSIS_DATABASE_EXECUTION_ROOT": "/analysis/databases",
        "ANALYSIS_RUN_EXECUTION_ROOT": "/analysis/runs",
        "ANALYSIS_INPUT_STAGING_EXECUTION_ROOT": "/analysis/input-staging",
    }
    host = {
        execution_key: str(host_paths[host_key])
        for execution_key, (host_key, _) in analysis_node.EXECUTION_ROOTS.items()
    }
    values.update(host if runtime == "host" else isolated)
    return values


def _image_values() -> dict[str, str]:
    return {
        environment_key: f"bioworkflowmanage/{role}:1.2.3"
        for role, environment_key in analysis_node.IMAGE_ENV_KEYS.items()
    }


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def test_analysis_node_config_validates_runtime_paths_and_secrets(tmp_path):
    isolated = _config(tmp_path)
    assert not analysis_node._failed(analysis_node.validate_environment(isolated))

    host = _config(tmp_path, runtime="host")
    assert not analysis_node._failed(analysis_node.validate_environment(host))

    host["ANALYSIS_RUN_EXECUTION_ROOT"] = "/analysis/runs"
    checks = analysis_node.validate_environment(host)
    assert any(item.name == "execution-paths" and item.status == "fail" for item in checks)

    isolated["POSTGRES_PASSWORD"] = "password"
    checks = analysis_node.validate_environment(isolated)
    assert any(item.name == "postgres-secret" and item.status == "fail" for item in checks)


def test_analysis_node_init_is_idempotent_and_preserves_existing_files(tmp_path):
    values = _config(tmp_path)
    data_root = Path(values["ANALYSIS_NODE_DATA_ROOT"])
    data_root.mkdir(parents=True)
    sentinel = data_root / "customer-data.txt"
    sentinel.write_text("keep", encoding="utf-8")
    context = analysis_node.Context(
        package_dir=tmp_path,
        env_file=tmp_path / ".env",
        images_env=tmp_path / "images.env",
        values=values,
        image_values=_image_values(),
    )

    analysis_node.initialize_directories(context)
    analysis_node.initialize_directories(context)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    marker = json.loads((data_root / ".analysis-node").read_text(encoding="utf-8"))
    assert marker["product_version"] == "1.2.3"
    for key in analysis_node.DIRECTORY_MODES:
        assert Path(values[key]).is_dir()


def test_analysis_node_checksum_verification_detects_tampering(tmp_path):
    payload = tmp_path / "payload.txt"
    payload.write_text("trusted\n", encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(
        f"{digest}  payload.txt\n",
        encoding="utf-8",
    )

    assert not analysis_node._failed(analysis_node.verify_checksums(tmp_path))
    payload.write_text("tampered\n", encoding="utf-8")
    assert analysis_node._failed(analysis_node.verify_checksums(tmp_path))


def test_analysis_node_image_lock_requires_all_digest_pinned_roles(tmp_path):
    values = _config(tmp_path)
    image_values = _image_values()
    document = {
        "schema_version": 1,
        "product_version": values["ANALYSIS_NODE_VERSION"],
        "platform": "linux/amd64",
        "images": {
            role: {
                "local_ref": image_values[analysis_node.IMAGE_ENV_KEYS[role]],
                "source_ref": f"registry.example/{role}@sha256:{'a' * 64}",
                "image_id": f"sha256:{'b' * 64}",
            }
            for role in analysis_node.REQUIRED_IMAGE_ROLES
        },
    }
    (tmp_path / "images.lock.json").write_text(json.dumps(document), encoding="utf-8")
    assert not analysis_node._failed(
        analysis_node.validate_image_lock(tmp_path, values, image_values)
    )

    del document["images"]["smoke-task"]
    (tmp_path / "images.lock.json").write_text(json.dumps(document), encoding="utf-8")
    assert analysis_node._failed(
        analysis_node.validate_image_lock(tmp_path, values, image_values)
    )


def test_analysis_node_env_parser_rejects_duplicate_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MODE=headless\nMODE=console\n", encoding="utf-8")
    with pytest.raises(analysis_node.AnalysisNodeError, match="重复定义"):
        analysis_node.parse_env_file(path)


def test_analysis_node_delivery_template_keeps_loopback_health_hosts():
    values = analysis_node.parse_env_file(
        PROJECT_ROOT / "deploy" / "analysis-node" / ".env.example"
    )
    allowed_hosts = set(values["DJANGO_ALLOWED_HOSTS"].split(","))
    assert {"localhost", "127.0.0.1"} <= allowed_hosts


def test_analysis_node_bundle_builder_requires_digest_pinned_sources():
    digest = "a" * 64
    release_document = {
        "schema_version": 1,
        "application_repositories": {
            "backend": "registry.example/backend",
            "frontend": "registry.example/frontend",
        },
        "third_party_images": {
            role: f"registry.example/{role}@sha256:{digest}"
            for role in bundle_builder.REQUIRED_ROLES[2:]
        },
    }
    sources = bundle_builder.validate_sources(
        "1.2.3",
        backend_ref=f"registry.example/backend@sha256:{digest}",
        frontend_ref=f"registry.example/frontend@sha256:{digest}",
        release_document=release_document,
    )
    assert set(sources) == set(bundle_builder.REQUIRED_ROLES)
    assert "ANALYSIS_NODE_SMOKE_TASK_IMAGE=bioworkflowmanage/smoke-task:1.2.3" in (
        bundle_builder.render_images_env("1.2.3")
    )

    with pytest.raises(bundle_builder.BundleError, match="固定 digest"):
        bundle_builder.validate_sources(
            "1.2.3",
            backend_ref="registry.example/backend:latest",
            frontend_ref=f"registry.example/frontend@sha256:{digest}",
            release_document=release_document,
        )


def test_analysis_node_smoke_wdl_is_syntactically_valid(tmp_path):
    snapshot = smoke_workflow_snapshot("bioworkflowmanage/smoke-task:test")
    path = tmp_path / "workflow.wdl"
    path.write_text(snapshot["bundle"]["files"]["workflow.wdl"], encoding="utf-8")
    document = WDL.load(str(path))
    assert document.workflow is not None
    assert document.workflow.name == "analysis_node_smoke"


@pytest.mark.django_db
def test_prepare_analysis_node_smoke_is_idempotent_and_refuses_drift(monkeypatch):
    monkeypatch.setenv(
        "ANALYSIS_NODE_SMOKE_TASK_IMAGE",
        "bioworkflowmanage/smoke-task:test",
    )
    first = StringIO()
    second = StringIO()

    call_command("prepare_analysis_node_smoke", stdout=first)
    call_command("prepare_analysis_node_smoke", stdout=second)

    assert "PUBLISHED analysis-node-smoke@1.0.0" in first.getvalue()
    assert "REUSED analysis-node-smoke@1.0.0" in second.getvalue()
    assert WorkflowVersion.objects.filter(
        workflow__slug="analysis-node-smoke",
        version=1,
    ).count() == 1
    assert AnalysisProductVersion.objects.filter(
        product__code="analysis-node-smoke",
        contract_version="1.0.0",
    ).count() == 1

    WorkflowDocument.objects.filter(slug="analysis-node-smoke").update(
        workflow_graph={"nodes": []}
    )
    with pytest.raises(CommandError, match="可信快照不一致"):
        call_command("prepare_analysis_node_smoke")

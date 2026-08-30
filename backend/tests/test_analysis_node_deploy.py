from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import WDL
from django.core.management import call_command
from django.core.management.base import CommandError

from workflows.management.commands.prepare_analysis_node_smoke import (
    smoke_workflow_snapshot,
)
from workflows.models import (
    AnalysisProductVersion,
    WorkflowDocument,
    WorkflowPackageAttestation,
    WorkflowVersion,
)


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
        "ANALYSIS_NODE_PROJECT_NAME": "analysis-node-test",
        "ANALYSIS_NODE_MODE": "headless",
        "ANALYSIS_NODE_RUNTIME": runtime,
        "ANALYSIS_NODE_BIND_ADDRESS": "127.0.0.1",
        "ANALYSIS_NODE_API_PORT": "18082",
        "ANALYSIS_NODE_CONSOLE_PORT": "18083",
        "ANALYSIS_NODE_PUBLIC_BASE_URL": "https://analysis.example.com",
        "ANALYSIS_NODE_MIN_FREE_GB": "0",
        "DJANGO_ALLOWED_HOSTS": "analysis.example.com,localhost,127.0.0.1",
        "DJANGO_CSRF_TRUSTED_ORIGINS": "https://analysis.example.com",
        "CORS_ALLOWED_ORIGINS": "https://analysis.example.com",
        "DJANGO_AUTH_REQUIRED": "1",
        "DJANGO_SESSION_COOKIE_SECURE": "1",
        "DJANGO_CSRF_COOKIE_SECURE": "1",
        "DJANGO_TRUSTED_PROXY_COUNT": "1",
        "POSTGRES_DB": "bioworkflow",
        "POSTGRES_USER": "bioworkflow",
        "POSTGRES_PASSWORD": "database-secret-123456",
        "DJANGO_SECRET_KEY": "django-secret-key-with-at-least-32-characters",
        "WEBHOOK_SIGNING_KEY": "webhook-secret-key-with-at-least-32-characters",
        "INTEGRATION_REQUIRE_ANALYSIS_PRODUCT": "1",
        "INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE": "1",
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
        "ANALYSIS_MIN_AVAILABLE_MEMORY_GB": "0",
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
        environment_key: (
            "bioworkflowmanage/smoke-task:1.0.0"
            if role == "smoke-task"
            else f"bioworkflowmanage/{role}:1.2.3"
        )
        for role, environment_key in analysis_node.IMAGE_ENV_KEYS.items()
    }


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def _context(tmp_path: Path, *, runtime: str = "isolated"):
    return analysis_node.Context(
        package_dir=tmp_path,
        env_file=tmp_path / ".env",
        images_env=tmp_path / "images.env",
        values=_config(tmp_path, runtime=runtime),
        image_values=_image_values(),
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

    invalid_proxy = _config(tmp_path)
    invalid_proxy["DJANGO_TRUSTED_PROXY_COUNT"] = "0"
    checks = analysis_node.validate_environment(invalid_proxy)
    assert any(
        item.name == "trusted-proxy-count" and item.status == "fail"
        for item in checks
    )


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


def test_analysis_node_preflight_helpers_detect_permissions_and_network_conflict(
    monkeypatch,
    tmp_path,
):
    context = _context(tmp_path)
    analysis_node.initialize_directories(context)
    context.values["MINIWDL_UID"] = str(os.geteuid() + 10000)
    context.values["MINIWDL_GID"] = str(os.getegid() + 10000)
    permission_checks = analysis_node._directory_permission_checks(context.values)
    assert any(
        item.name == "worker-directory-access" and item.status == "fail"
        for item in permission_checks
    )

    network = {
        "Name": "existing-lab-network",
        "Labels": {},
        "IPAM": {"Config": [{"Subnet": "10.253.0.0/25"}]},
    }

    def fake_run(command, **_kwargs):
        if command[:4] == ["docker", "network", "ls", "--quiet"]:
            return SimpleNamespace(stdout="network-id\n")
        return SimpleNamespace(stdout=json.dumps([network]))

    monkeypatch.setattr(analysis_node, "_run", fake_run)
    network_checks = analysis_node._docker_network_conflict_checks(context.values)
    assert network_checks[0].status == "fail"
    assert "existing-lab-network" in network_checks[0].message


def test_analysis_node_state_change_refuses_untrusted_runtime(monkeypatch, tmp_path):
    context = _context(tmp_path)
    called = False

    def refuse(_context):
        raise analysis_node.AnalysisNodeError("untrusted runtime")

    def unexpected_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(analysis_node, "require_trusted_runtime", refuse)
    monkeypatch.setattr(analysis_node, "_run", unexpected_run)

    with pytest.raises(analysis_node.AnalysisNodeError, match="untrusted"):
        analysis_node.compose_action(context, "migrate")
    assert called is False


def test_analysis_node_smoke_refuses_existing_probe_path_before_token_issue(
    monkeypatch,
    tmp_path,
):
    context = _context(tmp_path)
    analysis_node.initialize_directories(context)
    rawdata_root = Path(context.values["ANALYSIS_RAWDATA_HOST_PATH"])
    target = tmp_path / "untrusted-target"
    target.mkdir()
    (rawdata_root / ".analysis-node-smoke-smoke-fixed").symlink_to(
        target,
        target_is_directory=True,
    )
    monkeypatch.setattr(
        analysis_node,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed"),
    )

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("service token must not be issued")

    monkeypatch.setattr(analysis_node, "_run", unexpected_run)

    with pytest.raises(analysis_node.AnalysisNodeError, match="拒绝复用"):
        analysis_node.smoke(context, timeout_seconds=1)
    assert (rawdata_root / ".analysis-node-smoke-smoke-fixed").is_symlink()

    existing_directory = rawdata_root / ".analysis-node-smoke-smoke-existing"
    existing_directory.mkdir()
    with pytest.raises(analysis_node.AnalysisNodeError, match="拒绝复用"):
        analysis_node._prepare_smoke_probe(context, "smoke-existing")
    assert existing_directory.is_dir()


def test_analysis_node_restore_stops_writers_before_safety_backup(
    monkeypatch,
    tmp_path,
):
    context = _context(tmp_path)
    backup_root = Path(context.values["ANALYSIS_NODE_BACKUP_ROOT"])
    backup_root.mkdir()
    backup = backup_root / "restore.dump"
    backup.write_bytes(b"database-backup")
    backup.with_suffix(".json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product_version": context.values["ANALYSIS_NODE_VERSION"],
                "database": context.values["POSTGRES_DB"],
                "file": backup.name,
                "sha256": analysis_node._sha256(backup),
            }
        ),
        encoding="utf-8",
    )
    events = []

    monkeypatch.setattr(analysis_node, "require_trusted_runtime", lambda _context: [])

    def fake_run(command, **_kwargs):
        events.append(command)
        return SimpleNamespace(stdout="")

    def fake_backup(_context):
        events.append("safety-backup")
        return backup_root / "safety.dump"

    monkeypatch.setattr(analysis_node, "_run", fake_run)
    monkeypatch.setattr(analysis_node, "create_backup", fake_backup)
    monkeypatch.setattr(
        analysis_node.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    analysis_node.restore_backup(context, backup, confirmed=True)

    stop_index = events[0].index("stop")
    assert tuple(events[0][stop_index + 1 :]) == analysis_node.RESTORE_STOP_SERVICES
    assert {"analysis-worker-host", "analysis-worker-isolated"} <= set(events[0])
    assert events[1][-4:] == ["up", "-d", "--wait", "db"]
    assert events[2] == "safety-backup"


def test_analysis_node_restore_stop_targets_are_compose_services():
    compose_text = (
        PROJECT_ROOT / "deploy" / "analysis-node" / "compose.yml"
    ).read_text(encoding="utf-8")
    services_text = compose_text.split("\nservices:\n", 1)[1].split(
        "\nnetworks:\n",
        1,
    )[0]
    service_names = set(
        re.findall(r"^  ([a-z0-9-]+):$", services_text, re.MULTILINE)
    )

    assert set(analysis_node.RESTORE_STOP_SERVICES) <= service_names


def test_analysis_node_attests_only_after_offline_sigstore_verification(
    monkeypatch,
    tmp_path,
):
    context = _context(tmp_path)
    manifest = tmp_path / "workflow-package.json"
    signature = tmp_path / "workflow-package.sigstore.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow_version_id": 42,
                "source_digest": f"sha256:{'a' * 64}",
            }
        ),
        encoding="utf-8",
    )
    signature.write_text("{}\n", encoding="utf-8")
    commands = []
    monkeypatch.setattr(analysis_node, "require_trusted_runtime", lambda _context: [])
    monkeypatch.setattr(analysis_node.shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(analysis_node, "_run", fake_run)
    checks = analysis_node.attest_workflow_package(
        context,
        manifest_path=manifest,
        signature_bundle_path=signature,
        certificate_identity="https://github.com/example/release.yml@refs/tags/v1",
        certificate_oidc_issuer="https://token.actions.githubusercontent.com",
    )

    assert checks[0].status == "pass"
    assert commands[0][:3] == ["cosign", "verify-blob", "--offline"]
    assert "attest_workflow_package" in commands[1]
    assert commands[1].index("attest_workflow_package") > 0

    linked_manifest = tmp_path / "linked-workflow-package.json"
    linked_manifest.symlink_to(manifest)
    with pytest.raises(analysis_node.AnalysisNodeError, match="无法安全读取"):
        analysis_node.attest_workflow_package(
            context,
            manifest_path=linked_manifest,
            signature_bundle_path=signature,
            certificate_identity="https://github.com/example/release.yml@refs/tags/v1",
            certificate_oidc_issuer="https://token.actions.githubusercontent.com",
        )


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


def test_analysis_node_env_file_must_be_private_regular_file(tmp_path):
    env_file = tmp_path / ".env"
    images_env = tmp_path / "images.env"
    _write_env(env_file, _config(tmp_path))
    _write_env(images_env, _image_values())
    env_file.chmod(0o644)
    args = SimpleNamespace(
        package_dir=str(tmp_path),
        env_file=str(env_file),
        images_env=str(images_env),
    )

    with pytest.raises(analysis_node.AnalysisNodeError, match="chmod 0600"):
        analysis_node.build_context(args)

    env_file.chmod(0o600)
    assert analysis_node.build_context(args).env_file == env_file

    env_template = tmp_path / ".env.example"
    _write_env(env_template, _config(tmp_path))
    args.env_file = str(env_template)
    args.command = "verify-bundle"
    assert analysis_node.build_context(args).env_file == env_template

    linked_env = tmp_path / "linked.env"
    linked_env.symlink_to(env_file)
    with pytest.raises(analysis_node.AnalysisNodeError, match="无法安全读取"):
        analysis_node.parse_env_file(linked_env)


def test_analysis_node_delivery_template_keeps_loopback_health_hosts():
    values = analysis_node.parse_env_file(
        PROJECT_ROOT / "deploy" / "analysis-node" / ".env.example"
    )
    allowed_hosts = set(values["DJANGO_ALLOWED_HOSTS"].split(","))
    assert {"localhost", "127.0.0.1"} <= allowed_hosts
    assert values["DJANGO_TRUSTED_PROXY_COUNT"] == "2"


def test_analysis_node_gateways_normalize_forwarded_proto():
    for name in ("nginx-headless.conf", "nginx-console.conf"):
        config = (
            PROJECT_ROOT / "deploy" / "analysis-node" / name
        ).read_text(encoding="utf-8")
        assert "default $scheme;" in config
        assert "~*^https$ https;" in config
        assert "proxy_set_header X-Forwarded-Proto $scheme;" not in config
        assert (
            "proxy_set_header X-Forwarded-Proto $analysis_forwarded_proto;"
            in config
        )


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
    assert "ANALYSIS_NODE_SMOKE_TASK_IMAGE=bioworkflowmanage/smoke-task:1.0.0" in (
        bundle_builder.render_images_env("1.2.3")
    )

    with pytest.raises(bundle_builder.BundleError, match="固定 digest"):
        bundle_builder.validate_sources(
            "1.2.3",
            backend_ref="registry.example/backend:latest",
            frontend_ref=f"registry.example/frontend@sha256:{digest}",
            release_document=release_document,
        )


def test_analysis_node_bundle_builder_renders_versioned_offline_contract(
    monkeypatch,
    tmp_path,
):
    digest = "a" * 64
    package_source = tmp_path / "package-source"
    shutil.copytree(
        bundle_builder.PACKAGE_SOURCE,
        package_source,
        ignore=shutil.ignore_patterns(".env", "images.env"),
    )
    (package_source / ".env").write_text(
        "LOCAL_SECRET=must-not-ship\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bundle_builder, "PACKAGE_SOURCE", package_source)

    def fake_prepare_images(package_dir, *, version, platform_name, sources):
        assert version == "1.2.3"
        assert platform_name == "linux/amd64"
        (package_dir / "images.tar").write_bytes(b"offline-images")
        sbom = package_dir / "sbom"
        sbom.mkdir()
        images = {}
        for role in bundle_builder.REQUIRED_ROLES:
            (sbom / f"{role}.spdx.json").write_text("{}\n", encoding="utf-8")
            images[role] = {
                "local_ref": bundle_builder.local_reference(role, version),
                "source_ref": sources[role],
                "image_id": f"sha256:{digest}",
            }
        return images

    monkeypatch.setattr(bundle_builder, "_prepare_images", fake_prepare_images)
    signatures = tmp_path / "signatures"
    signatures.mkdir()
    (signatures / "backend.sigstore.json").write_text("{}\n", encoding="utf-8")
    output = tmp_path / "dist"
    args = SimpleNamespace(
        version="1.2.3",
        backend_ref=f"ghcr.io/qzhqzh/bioworkflowmanage-backend@sha256:{digest}",
        frontend_ref=f"ghcr.io/qzhqzh/bioworkflowmanage-frontend@sha256:{digest}",
        output_dir=str(output),
        platform="linux/amd64",
        git_revision="test-revision",
        signature_dir=str(signatures),
    )

    package, archive = bundle_builder.build_bundle(args)

    lock = json.loads((package / "images.lock.json").read_text(encoding="utf-8"))
    assert lock["product_version"] == "1.2.3"
    assert lock["images"]["smoke-task"]["local_ref"] == (
        "bioworkflowmanage/smoke-task:1.0.0"
    )
    assert "ANALYSIS_NODE_VERSION=1.2.3" in (
        package / ".env.example"
    ).read_text(encoding="utf-8")
    assert not (package / ".env").exists()
    assert all(
        b"must-not-ship" not in path.read_bytes()
        for path in package.rglob("*")
        if path.is_file()
    )
    assert archive.is_file()
    assert not analysis_node._failed(analysis_node.verify_checksums(package))


def test_analysis_node_smoke_wdl_is_syntactically_valid(tmp_path):
    snapshot = smoke_workflow_snapshot("bioworkflowmanage/smoke-task:test")
    path = tmp_path / "workflow.wdl"
    path.write_text(snapshot["bundle"]["files"]["workflow.wdl"], encoding="utf-8")
    document = WDL.load(str(path))
    assert document.workflow is not None
    assert document.workflow.name == "analysis_node_smoke"
    tool_node = next(
        item for item in snapshot["graph"]["nodes"] if item["type"] == "tool"
    )
    assert analysis_node.IMAGE_ID_PATTERN.fullmatch(
        tool_node["tool_ref"]["digest"]
    )


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
    assert WorkflowPackageAttestation.objects.filter(
        workflow_version__workflow__slug="analysis-node-smoke",
        verification_method=WorkflowPackageAttestation.VerificationMethod.BUNDLED,
    ).count() == 1

    WorkflowDocument.objects.filter(slug="analysis-node-smoke").update(
        workflow_graph={"nodes": []}
    )
    with pytest.raises(CommandError, match="可信快照不一致"):
        call_command("prepare_analysis_node_smoke")


@pytest.mark.django_db
def test_prepare_analysis_node_smoke_rejects_unpinned_image(monkeypatch):
    monkeypatch.setenv(
        "ANALYSIS_NODE_SMOKE_TASK_IMAGE",
        "registry.example:5000/smoke-task",
    )
    with pytest.raises(CommandError, match="必须固定"):
        call_command("prepare_analysis_node_smoke")

    monkeypatch.setenv(
        "ANALYSIS_NODE_SMOKE_TASK_IMAGE",
        "registry.example/smoke-task:latest",
    )
    with pytest.raises(CommandError, match="必须固定"):
        call_command("prepare_analysis_node_smoke")

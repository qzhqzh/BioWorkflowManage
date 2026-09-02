from __future__ import annotations

import json
import stat
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from compiler_core import canonical_digest
from workflows.analysis_runtime import _materialize_source, claim_next_run, execute_analysis_run
from workflows.execution_engines import (
    MINIWDL,
    NEXTFLOW,
    NEXTFLOW_ENGINE_VERSION,
    ExecutionSnapshotError,
    validate_execution_snapshot,
)
from workflows.integration_api import IntegrationAPIError, _analysis_source
from workflows.models import (
    AnalysisProductVersion,
    AnalysisRun,
    WorkflowDocument,
    WorkflowVersion,
)
from workflows.nextflow_runtime import (
    _collect_outputs,
    _nextflow_environment,
    _nextflow_arguments,
    _write_nextflow_config,
    cleanup_nextflow_containers_for_run,
)


pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("auth_disabled")]

PINNED_IMAGE = (
    "registry.example.test/lc103@"
    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
)


def _runtime_manifest() -> dict:
    return {
        "schema_version": 1,
        "engine_version": "25.04.8",
        "profile": "lc103-amp-v1",
        "input_adapter": {
            "kind": "paired_fastq_csv",
            "read1": "read1",
            "read2": "read2",
        },
        "fixed_params": {"panel": "LC103"},
        "path_params": [],
        "database_relative_path": ".",
        "container_images": {
            "default": PINNED_IMAGE,
            "labels": {"fastp": PINNED_IMAGE},
            "processes": {"Fastp": PINNED_IMAGE},
        },
        "outputs": [
            {"name": "qc_report", "glob": "reportResults/*QC.xlsx", "required": True},
            {
                "name": "variants",
                "glob": "reportResults/*variants.tsv",
                "required": True,
            },
        ],
    }


def _nextflow_version(*, slug: str = "lc103-nextflow") -> WorkflowVersion:
    runtime_manifest = _runtime_manifest()
    graph = {
        "id": "lc103_amp",
        "nodes": [
            {"id": "read1", "type": "workflow_input"},
            {"id": "read2", "type": "workflow_input"},
            {"id": "qc_report", "type": "workflow_output"},
            {"id": "variants", "type": "workflow_output"},
        ],
        "edges": [],
    }
    interface = {
        "inputs": [
            {
                "name": "read1",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r1",
                "required": True,
            },
            {
                "name": "read2",
                "wdl_type": "File",
                "semantic_type": "bio.fastq.gz.r2",
                "required": True,
            },
        ],
        "outputs": [
            {
                "name": "qc_report",
                "wdl_type": "File",
                "semantic_type": "report.qc_xlsx",
                "required": True,
            },
            {
                "name": "variants",
                "wdl_type": "File",
                "semantic_type": "report.variants_tsv",
                "required": True,
            },
        ],
    }
    bundle = {
        "entrypoint": "main.nf",
        "files": {"main.nf": "nextflow.enable.dsl=2\nworkflow {}\n"},
        "executable_files": [],
        "call_count": 1,
        "execution": {"engine": NEXTFLOW, "runtime_manifest": runtime_manifest},
    }
    document = WorkflowDocument.objects.create(
        slug=slug,
        name="LC103",
        workflow_graph=graph,
    )
    return WorkflowVersion.objects.create(
        workflow=document,
        version=1,
        name="LC103",
        semantic_digest=canonical_digest(graph),
        workflow_graph=graph,
        compiled_bundle=bundle,
        compiled_digest=canonical_digest(bundle),
        execution_engine=NEXTFLOW,
        runtime_manifest=runtime_manifest,
        interface_contract=interface,
    )


def _nextflow_run(version: WorkflowVersion, *, sample_id: str = "S001") -> AnalysisRun:
    return AnalysisRun.objects.create(
        workflow_version=version,
        workflow_name="lc103_amp",
        sample_id=sample_id,
        source_bundle=version.compiled_bundle,
        source_digest=version.compiled_digest,
        execution_engine=NEXTFLOW,
        runtime_manifest=version.runtime_manifest,
        request_payload={
            "integration_output_contract": [
                {"name": "qc_report", "wdl_type": "File"},
                {"name": "variants", "wdl_type": "File"},
            ]
        },
    )


def test_nextflow_snapshot_requires_digest_pinned_container():
    runtime_manifest = _runtime_manifest()
    runtime_manifest["container_images"]["default"] = "registry.example.test/lc103:latest"
    bundle = {
        "entrypoint": "main.nf",
        "files": {"main.nf": "workflow {}"},
        "call_count": 1,
        "execution": {"engine": NEXTFLOW, "runtime_manifest": runtime_manifest},
    }

    with pytest.raises(ExecutionSnapshotError, match="repo@sha256"):
        validate_execution_snapshot(
            NEXTFLOW,
            bundle,
            runtime_manifest,
            output_names={"qc_report", "variants"},
        )


def test_nextflow_snapshot_requires_phase_one_engine_version():
    runtime_manifest = _runtime_manifest()
    runtime_manifest["engine_version"] = "25.10.0"
    bundle = {
        "entrypoint": "main.nf",
        "files": {"main.nf": "workflow {}"},
        "call_count": 1,
        "execution": {"engine": NEXTFLOW, "runtime_manifest": runtime_manifest},
    }

    with pytest.raises(ExecutionSnapshotError, match=NEXTFLOW_ENGINE_VERSION):
        validate_execution_snapshot(
            NEXTFLOW,
            bundle,
            runtime_manifest,
            output_names={"qc_report", "variants"},
        )


def test_workers_claim_only_their_explicit_engine(settings):
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 0
    nextflow_version = _nextflow_version()
    nextflow_run = _nextflow_run(nextflow_version)
    document = WorkflowDocument.objects.create(slug="miniwdl", name="MiniWDL")
    miniwdl_bundle = {
        "entrypoint": "workflow.wdl",
        "files": {"workflow.wdl": "version 1.0\nworkflow Mini {}\n"},
    }
    miniwdl_version = WorkflowVersion.objects.create(
        workflow=document,
        version=1,
        name="MiniWDL",
        semantic_digest="sha256:" + "2" * 64,
        workflow_graph={"id": "Mini", "nodes": [], "edges": []},
        compiled_bundle=miniwdl_bundle,
        compiled_digest=canonical_digest(miniwdl_bundle),
        interface_contract={"inputs": [], "outputs": []},
    )
    miniwdl_run = AnalysisRun.objects.create(
        workflow_version=miniwdl_version,
        workflow_name="Mini",
        sample_id="MINI",
        source_bundle=miniwdl_bundle,
        source_digest=miniwdl_version.compiled_digest,
    )

    claimed_miniwdl = claim_next_run()
    claimed_nextflow = claim_next_run((NEXTFLOW,))

    assert claimed_miniwdl.pk == miniwdl_run.pk
    assert claimed_nextflow.pk == nextflow_run.pk
    assert claimed_miniwdl.execution_engine == MINIWDL
    assert claimed_nextflow.execution_engine == NEXTFLOW


def test_miniwdl_worker_does_not_recover_stale_nextflow_run(settings):
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 0
    version = _nextflow_version()
    run = _nextflow_run(version)
    AnalysisRun.objects.filter(pk=run.pk).update(
        status=AnalysisRun.Status.PREPARING,
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    assert claim_next_run() is None

    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.PREPARING


def test_execute_dispatches_to_nextflow_adapter(monkeypatch):
    version = _nextflow_version()
    run = _nextflow_run(version)
    dispatched = []

    monkeypatch.setattr(
        "workflows.nextflow_runtime.execute_nextflow_analysis_run",
        lambda item, heartbeat: dispatched.append((item.pk, heartbeat)),
    )

    execute_analysis_run(run)

    assert dispatched == [(run.pk, None)]


def test_nextflow_direct_workflow_submission_requires_analysis_product(settings):
    settings.INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE = False
    settings.INTEGRATION_REQUIRE_ANALYSIS_PRODUCT = False
    version = _nextflow_version()

    with pytest.raises(IntegrationAPIError) as captured:
        _analysis_source(
            {
                "workflow": {
                    "source_type": "workflow_version",
                    "version_id": version.pk,
                    "expected_source_digest": version.compiled_digest,
                }
            }
        )

    assert captured.value.code == "ANALYSIS_PRODUCT_REQUIRED"


def test_materialize_nextflow_package_restores_binary_and_executable(tmp_path):
    version = _nextflow_version()
    bundle = dict(version.compiled_bundle)
    bundle["files"] = {
        **bundle["files"],
        "bin/tool.py": "#!/usr/bin/env python3\n",
        "assets/table.xlsx": {
            "encoding": "base64",
            "content": "UEsDBA==",
        },
    }
    bundle["executable_files"] = ["bin/tool.py"]
    WorkflowVersion.objects.filter(pk=version.pk).update(
        compiled_bundle=bundle,
        compiled_digest=canonical_digest(bundle),
    )
    version.compiled_bundle = bundle
    version.compiled_digest = canonical_digest(bundle)
    run = _nextflow_run(version)
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    entrypoint = _materialize_source(run, run_directory)

    assert entrypoint.name == "main.nf"
    assert (run_directory / "source/assets/table.xlsx").read_bytes() == b"PK\x03\x04"
    assert stat.S_IMODE((run_directory / "source/bin/tool.py").stat().st_mode) == 0o755


def test_nextflow_arguments_never_enable_implicit_resume(tmp_path):
    manifest = _runtime_manifest()
    arguments = _nextflow_arguments(
        "/usr/local/bin/nextflow",
        entrypoint=tmp_path / "source/main.nf",
        config_path=tmp_path / "nextflow.config",
        fastq_list=tmp_path / "fastq-list.csv",
        source_directory=tmp_path / "source",
        database_path=tmp_path / "database",
        attempt_directory=tmp_path / "attempt-1",
        manifest=manifest,
        task_name="S001",
    )

    assert "-resume" not in arguments
    assert arguments[1:4] == ["-C", str(tmp_path / "nextflow.config"), "run"]
    panel_index = arguments.index("--panel")
    assert arguments[panel_index + 1] == "LC103"


def test_nextflow_environment_does_not_inherit_application_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "database-secret")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "django-secret")
    monkeypatch.setenv("DOCKER_HOST", "tcp://nextflow-docker:2376")

    environment = _nextflow_environment(tmp_path / "run")

    assert "POSTGRES_PASSWORD" not in environment
    assert "DJANGO_SECRET_KEY" not in environment
    assert environment["DOCKER_HOST"] == "tcp://nextflow-docker:2376"
    assert environment["NXF_OFFLINE"] == "true"


def test_nextflow_config_pins_images_and_run_label(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "nextflow.config").write_text("docker.enabled = false\n", encoding="utf-8")
    config = tmp_path / "generated.config"

    _write_nextflow_config(
        config,
        run_id="run-123",
        run_execution_directory=tmp_path,
        manifest=_runtime_manifest(),
    )

    content = config.read_text(encoding="utf-8")
    assert PINNED_IMAGE in content
    assert "bioworkflow.analysis_run_id=run-123" in content
    assert "resume = false" in content
    assert "withName: 'Fastp'" in content
    assert "includeConfig" not in content
    assert "executor = 'local'" in content
    assert "containerOptions = ''" in content


def test_nextflow_output_collection_uses_contract_keys(tmp_path, settings):
    settings.ANALYSIS_RUN_ROOT = tmp_path
    settings.ANALYSIS_RUN_EXECUTION_ROOT = tmp_path
    version = _nextflow_version()
    run = _nextflow_run(version)
    results = tmp_path / str(run.id) / "attempt-1/results/reportResults"
    results.mkdir(parents=True)
    (results / "S001.QC.xlsx").write_bytes(b"xlsx")
    (results / "S001_all_samples_variants.tsv").write_text(
        "CHROM\tPOS\n",
        encoding="utf-8",
    )

    result = _collect_outputs(run, results.parent, version.runtime_manifest)

    assert set(result["outputs"]) == {
        "lc103_amp.qc_report",
        "lc103_amp.variants",
    }
    assert result["outputs"]["lc103_amp.qc_report"].endswith("S001.QC.xlsx")


def test_import_nextflow_product_is_idempotent(tmp_path, settings):
    settings.INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE = False
    source = tmp_path / "source"
    (source / "bin").mkdir(parents=True)
    (source / "main.nf").write_text("nextflow.enable.dsl=2\nworkflow {}\n", encoding="utf-8")
    (source / "bin/tool.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (source / "asset.bin").write_bytes(b"\xff\x00")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "pytest@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Pytest"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-q", "-m", "fixture"],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest = {
        "schema_version": 1,
        "workflow": {
            "slug": "imported-lc103",
            "workflow_id": "lc103_amp",
            "name": "LC103 AMP",
        },
        "analysis_product": {
            "code": "lc103-amp",
            "name": "LC103 AMP",
            "contract_version": "1.0.0",
        },
        "source": {
            "repository": "ssh://git@example.test/lc103.git",
            "revision": revision,
            "entrypoint": "main.nf",
            "call_count": 1,
            "files": ["main.nf", "bin/tool.py", "asset.bin"],
            "executable_files": ["bin/tool.py"],
        },
        "runtime_manifest": _runtime_manifest(),
        "interface_contract": {
            "inputs": [
                {
                    "name": "read1",
                    "wdl_type": "File",
                    "semantic_type": "bio.fastq.gz.r1",
                },
                {
                    "name": "read2",
                    "wdl_type": "File",
                    "semantic_type": "bio.fastq.gz.r2",
                },
            ],
            "outputs": [
                {
                    "name": "qc_report",
                    "wdl_type": "File",
                    "semantic_type": "report.qc_xlsx",
                },
                {
                    "name": "variants",
                    "wdl_type": "File",
                    "semantic_type": "report.variants_tsv",
                },
            ],
        },
    }
    manifest_path = tmp_path / "product.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    for _ in range(2):
        call_command(
            "import_nextflow_product",
            manifest=str(manifest_path),
            source_dir=str(source),
            publish_product=True,
        )

    version = WorkflowVersion.objects.get(workflow__slug="imported-lc103")
    assert version.execution_engine == NEXTFLOW
    assert version.compiled_bundle["files"]["asset.bin"]["encoding"] == "base64"
    assert AnalysisProductVersion.objects.get(
        product__code="lc103-amp",
        contract_version="1.0.0",
    ).workflow_version == version

    (source / "main.nf").write_text("workflow { dirty = true }\n", encoding="utf-8")
    with pytest.raises(CommandError, match="未提交改动"):
        call_command(
            "import_nextflow_product",
            manifest=str(manifest_path),
            source_dir=str(source),
            dry_run=True,
        )


class _FakeContainer:
    def __init__(self, name: str, source: Path):
        self.name = name
        self.id = name
        self.status = "running"
        self.attrs = {"Mounts": [{"Source": str(source)}]}
        self.stopped = False
        self.removed = False

    def stop(self, timeout):
        self.stopped = timeout == 5

    def remove(self, force):
        self.removed = force


class _FakeContainers:
    def __init__(self, values):
        self.values = values
        self.filters = None

    def list(self, *, all, filters):
        assert all is True
        self.filters = filters
        return self.values


class _FakeDockerClient:
    def __init__(self, values):
        self.containers = _FakeContainers(values)


def test_nextflow_cleanup_requires_label_and_run_mount(tmp_path):
    run_directory = tmp_path / "run"
    work = run_directory / "attempt-1/work"
    work.mkdir(parents=True)
    own = _FakeContainer("own", work)
    foreign = _FakeContainer("foreign", tmp_path / "other")
    client = _FakeDockerClient([own, foreign])

    removed, errors = cleanup_nextflow_containers_for_run(
        run_directory,
        "run-123",
        docker_client=client,
    )

    assert removed == ["own"]
    assert errors == []
    assert own.stopped and own.removed
    assert not foreign.stopped and not foreign.removed
    assert client.containers.filters == {
        "label": "bioworkflow.analysis_run_id=run-123"
    }

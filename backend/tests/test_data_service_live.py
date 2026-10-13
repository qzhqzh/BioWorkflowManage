"""Opt-in HTTP and migration contract tests against a separately installed data service."""

import gzip
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from django.core.management import call_command

from workflows.models import AnalysisRun, RawdataDatasetIndex
from workflows.rawdata_readiness import rawdata_readiness
from test_integration_api import _submission, _token_client, _workflow_version
from test_integration_api import integration_workspace as integration_workspace
from test_rawdata_catalog import _finish_index
from test_rawdata_readiness import validate_response as validate_response

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        not os.environ.get("DATA_SERVICE_SOURCE"),
        reason="Set DATA_SERVICE_SOURCE for the independent service contract tests",
    ),
]


def service_command(source, env, *args):
    result = subprocess.run(
        [str(source / ".venv/bin/python"), "manage.py", *args],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return result.stdout


@pytest.fixture
def service(tmp_path):
    source = Path(os.environ["DATA_SERVICE_SOURCE"])
    root = tmp_path / "service-rawdata"
    root.mkdir()
    for mate in (1, 2):
        with gzip.open(root / f"S001_R{mate}.fastq.gz", "wt") as handle:
            handle.write(f"@read/{mate}\nACGT\n+\n!!!!\n")
    (root / "_READY.done").touch()
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "data_service.settings",
        "DATA_SQLITE_PATH": str(tmp_path / "service.sqlite3"),
        "DATA_RAWDATA_ROOT": str(root),
        "DATA_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "DATA_POSTGRES_HOST": "",
        "DATA_ROOT_KEY": "",
    }
    env.pop("PYTHONPATH", None)
    service_command(source, env, "migrate", "--noinput")
    token = tmp_path / "data-token"
    service_command(
        source,
        env,
        "create_service_token",
        "bwm-test",
        "--directory",
        ".",
        "--scope",
        "data:read",
        "--scope",
        "data:verify",
        "--scope",
        "data:scan",
        "--token-file",
        str(token),
    )
    service_command(source, env, "run_indexer", "--once")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with (tmp_path / "service.log").open("w") as log:
        process = subprocess.Popen(
            [
                str(source / ".venv/bin/python"),
                "-m",
                "gunicorn",
                "data_service.wsgi:application",
                "--bind",
                f"127.0.0.1:{port}",
                "--workers",
                "1",
            ],
            cwd=source,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError(
                        "Independent service exited before becoming healthy"
                    )
                try:
                    with urlopen(
                        f"http://127.0.0.1:{port}/healthz", timeout=0.5
                    ) as response:
                        if response.status == 200:
                            break
                except (URLError, OSError):
                    time.sleep(0.1)
            else:
                raise AssertionError("Independent service did not become healthy")
            yield {
                "source": source,
                "env": env,
                "root": root,
                "url": f"http://127.0.0.1:{port}",
                "token": token,
                "process": process,
            }
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_real_http_catalog_submission_and_execution(
    service, settings, integration_workspace, validate_response
):
    from workflows.analysis_runtime import _verify_run_resource_manifests

    settings.DATA_SERVICE_URL = service["url"]
    settings.DATA_SERVICE_TOKEN_FILE = str(service["token"])
    settings.ANALYSIS_RAWDATA_ROOT = service["root"]
    settings.ANALYSIS_RAWDATA_EXECUTION_ROOT = service["root"]
    _, _, _, client = _token_client()
    catalog = client.get("/api/v1/integration/rawdata-datasets?include_unready=true")
    assert catalog.status_code == 200, catalog.data
    validate_response(catalog, "/rawdata-datasets")
    assert catalog.data["count"] == 1
    assert not RawdataDatasetIndex.objects.exists(), (
        "BWM must not populate its legacy data tables"
    )
    from workflows import data_service
    from jsonschema import Draft202012Validator

    native = data_service.request("GET", "/api/v1/datasets?include_unready=true")
    contract = json.loads((service["source"] / "schemas/openapi-v1.json").read_text())
    Draft202012Validator(
        {**contract, "$ref": "#/components/schemas/RawdataDatasetReadinessList"}
    ).validate(native)
    snapshot = catalog.data["results"][0]["readiness"]["snapshot"]
    body = _submission(_workflow_version())
    body["rawdata_readiness"] = snapshot
    preflight = client.post(
        "/api/v1/integration/analysis-runs/preflight", body, format="json"
    )
    assert preflight.status_code == 200, preflight.data
    response = client.post(
        "/api/v1/integration/analysis-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="split-service-test",
    )
    assert response.status_code == 201, response.data
    replay = client.post(
        "/api/v1/integration/analysis-runs",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="split-service-test",
    )
    assert replay.status_code == 200 and replay["Idempotency-Replayed"] == "true"
    assert AnalysisRun.objects.count() == 1
    run = AnalysisRun.objects.get()
    assert run.request_payload["rawdata_readiness"] == snapshot
    run.request_payload["dataset"] = catalog.data["results"][0]["dataset_id"]
    run.save(update_fields=["request_payload"])
    history = data_service.catalog()["datasets"][0]
    assert history["run_count"] == 1
    assert history["recent_runs"][0]["id"] == str(run.id)
    _verify_run_resource_manifests(run)
    (service["root"] / "_READY.done").unlink()
    with pytest.raises(ValueError, match="RAWDATA_NOT_READY"):
        _verify_run_resource_manifests(run)
    service["process"].terminate()
    service["process"].wait(timeout=10)
    unavailable = client.get("/api/v1/integration/rawdata-datasets")
    assert unavailable.status_code == 503
    assert unavailable.data["error"]["code"] == "DATA_SERVICE_UNAVAILABLE"
    assert unavailable.data["error"]["retryable"] is True


def test_export_import_preserves_changed_latch_and_identity(
    service, settings, tmp_path
):
    from workflows.rawdata_index import rawdata_root_key

    source, root = service["source"], service["root"]
    _finish_index(settings, root)
    files = ["S001_R1.fastq.gz", "S001_R2.fastq.gz"]
    original = RawdataDatasetIndex.objects.get()
    assert rawdata_readiness(files)["submission_allowed"]
    with (root / files[0]).open("ab") as output:
        output.write(b"changed")
    assert rawdata_readiness(files)["status"] == "changed"
    export = tmp_path / "export.json"
    call_command("export_data_service", output=str(export))
    env = {
        **service["env"],
        "DATA_SQLITE_PATH": str(tmp_path / "import.sqlite3"),
        "DATA_ROOT_KEY": rawdata_root_key(),
    }
    service_command(source, env, "migrate", "--noinput")
    service_command(source, env, "import_bwm_data", str(export))
    inspect = "from data_service.models import RawdataDatasetIndex; print(RawdataDatasetIndex.objects.count())"
    assert service_command(source, env, "shell", "-c", inspect).strip().endswith("0")
    service_command(source, env, "import_bwm_data", str(export), "--apply")
    service_command(source, env, "run_indexer", "--once")
    script = (
        "from data_service.rawdata_readiness import rawdata_readiness; from data_service.models import RawdataDatasetIndex; import json; print(json.dumps({'status': rawdata_readiness("
        + repr(files)
        + ")[\"status\"], 'id': RawdataDatasetIndex.objects.get().dataset_id}))"
    )
    result = json.loads(
        service_command(source, env, "shell", "-c", script).splitlines()[-1]
    )
    assert result == {"status": "changed", "id": original.dataset_id}
    duplicate = subprocess.run(
        [
            str(source / ".venv/bin/python"),
            "manage.py",
            "import_bwm_data",
            str(export),
            "--apply",
        ],
        cwd=source,
        env=env,
        capture_output=True,
    )
    assert duplicate.returncode != 0 and b"not empty" in duplicate.stderr


@pytest.mark.skipif(
    not os.environ.get("OKBOX_API_SOURCE"),
    reason="Set OKBOX_API_SOURCE for upstream integration",
)
def test_okbox_reads_data_without_analysis_api(service, tmp_path):
    api_source = Path(os.environ["OKBOX_API_SOURCE"])
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings_test",
        "PYTHONPATH": str(api_source / "src") + os.pathsep + str(api_source),
        "DJANGO_DATA_SERVICE_URL": service["url"],
        "DJANGO_DATA_SERVICE_TOKEN_FILE": str(service["token"]),
        "DJANGO_BIOWORKFLOW_BASE_URL": "http://127.0.0.1:1",
    }
    script = """import django, json
from unittest.mock import patch
django.setup()
from apps.console.sample.rawdata import catalog, checked_group
with patch('apps.console.sample.rawdata.BioWorkflowClient', side_effect=AssertionError('BWM must not be called')):
    result = catalog()
    group = result['results'][0]
    checked, files = checked_group(group['group_id'], group['identity_digest'])
    print(json.dumps({'ready': checked['record_allowed'], 'files': files}))
"""
    completed = subprocess.run(
        [str(api_source / ".venv/bin/python"), "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result == {"ready": True, "files": ["S001_R1.fastq.gz", "S001_R2.fastq.gz"]}

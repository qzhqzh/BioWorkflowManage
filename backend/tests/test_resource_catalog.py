from __future__ import annotations

import hashlib
import json

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from rest_framework.test import APIClient

from workflows.models import (
    AnalysisResourceCatalog,
    AnalysisResourceCatalogRevision,
)
from workflows.resource_catalog import (
    catalog_digest,
    entry_requirements,
    validate_catalog,
)


pytestmark = pytest.mark.usefixtures("auth_disabled")


@pytest.fixture
def resource_catalog_workspace(settings, tmp_path):
    databases = tmp_path / "databases"
    databases.mkdir()
    document = {
        "schema_version": 1,
        "references": [
            {
                "id": "hg19",
                "name": "hg19",
                "ref_version": "hg19",
                "required": [],
            }
        ],
        "panels": [],
    }
    catalog = databases / "catalog.json"
    catalog.write_text(json.dumps(document), encoding="utf-8")
    settings.ANALYSIS_DATABASE_ROOT = databases
    settings.ANALYSIS_DATABASE_CATALOG = catalog
    return databases, document


@pytest.mark.django_db
def test_catalog_file_can_be_managed_with_concurrency_and_audit(
    client, resource_catalog_workspace
):
    initial = client.get("/api/v1/resource-catalog")
    assert initial.status_code == 200
    assert initial.data["source"] == "file"
    assert initial.data["version"] == 0

    missing_precondition = client.put(
        "/api/v1/resource-catalog",
        {"document": initial.data["document"]},
        content_type="application/json",
    )
    assert missing_precondition.status_code == 428

    document = initial.data["document"]
    document["panels"].append(
        {
            "id": "panel-a",
            "name": "Panel A",
            "reference": "hg19",
            "workflow_ids": ["workflow-a"],
            "bindings": {"bed": "hg19/panels/a.bed"},
            "required_bindings": [
                {"key": "bed", "label": "Panel BED", "kind": "file"}
            ],
            "required": [],
        }
    )
    saved = client.put(
        "/api/v1/resource-catalog",
        {
            "document": document,
            "base_version": initial.data["version"],
            "base_digest": initial.data["digest"],
            "note": "新增 Panel A",
        },
        content_type="application/json",
    )
    assert saved.status_code == 200
    assert saved.data["source"] == "managed"
    assert saved.data["version"] == 1
    assert saved.data["panels"][0]["ready"] is False
    assert saved.data["panels"][0]["missing"][0]["path"] == "hg19/panels/a.bed"
    assert AnalysisResourceCatalog.objects.count() == 1
    revision = AnalysisResourceCatalogRevision.objects.get()
    assert revision.note == "新增 Panel A"
    assert revision.changes["panels"]["created"] == ["panel-a"]

    stale = client.put(
        "/api/v1/resource-catalog",
        {
            "document": document,
            "base_version": 0,
            "base_digest": initial.data["digest"],
        },
        content_type="application/json",
    )
    assert stale.status_code == 409
    assert stale.data["error"]["code"] == "RESOURCE_CATALOG_CONFLICT"


@pytest.mark.django_db
def test_catalog_rejects_paths_outside_database_root(client, resource_catalog_workspace):
    initial = client.get("/api/v1/resource-catalog").data
    document = initial["document"]
    document["references"][0]["required"] = [
        {"path": "../secret", "kind": "file", "label": "secret"}
    ]

    response = client.put(
        "/api/v1/resource-catalog",
        {
            "document": document,
            "base_version": initial["version"],
            "base_digest": initial["digest"],
        },
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "RESOURCE_CATALOG_PATH_INVALID"


@pytest.mark.django_db
def test_catalog_rejects_duplicate_or_invalid_binding_definitions(
    client, resource_catalog_workspace
):
    initial = client.get("/api/v1/resource-catalog").data
    document = initial["document"]
    document["references"][0]["required_bindings"] = [
        {"key": "fasta", "label": "FASTA", "kind": "file"},
        {"key": "fasta", "label": "FASTA index", "kind": "socket"},
    ]

    response = client.put(
        "/api/v1/resource-catalog",
        {
            "document": document,
            "base_version": initial["version"],
            "base_digest": initial["digest"],
        },
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.data["error"]["code"] == "RESOURCE_CATALOG_DUPLICATE"


@pytest.mark.django_db
def test_migration_command_adds_blood_tumor_reference_and_panels(
    resource_catalog_workspace,
):
    call_command("migrate_resource_catalog", actor="pytest")

    state = AnalysisResourceCatalog.objects.get(key="default")
    assert {item["id"] for item in state.document["references"]} == {"hg19", "hg38"}
    panels = {item["id"]: item for item in state.document["panels"]}
    assert set(panels) == {"blood-84", "blood-396", "blood-624"}
    assert panels["blood-84"]["bindings"]["bed"] == ""
    assert panels["blood-84"]["resource_version"] == ""
    assert panels["blood-84"]["workflow_ids"] == [
        "tumor-blood-single-production",
        "tumor-blood-pair-production",
    ]
    reference_paths = {
        item["path"] for item in state.document["references"][1]["required"]
    }
    panel_paths = {item["path"] for item in panels["blood-84"]["required"]}
    assert "hg38/blood_tumor/cnvkit/396" not in reference_paths
    assert "hg38/blood_tumor/resource/624panel_anno_filter.xls" in reference_paths
    assert "hg38/blood_tumor/resource/84panel_filter.xls" in panel_paths
    bed_binding = next(
        item
        for item in panels["blood-84"]["required_bindings"]
        if item["key"] == "bed"
    )
    assert bed_binding["basename_includes"] == ["84panel"]


@pytest.mark.django_db
def test_resource_migration_updates_templates_without_overwriting_panel_paths(
    resource_catalog_workspace,
):
    call_command("migrate_resource_catalog", actor="pytest")
    state = AnalysisResourceCatalog.objects.get(key="default")
    document = state.document
    panel = next(item for item in document["panels"] if item["id"] == "blood-84")
    panel["bindings"]["bed"] = "hg38/custom/84-production.bed"
    panel["resource_version"] = "production-2026"
    panel["workflow_ids"] = ["custom-workflow"]
    panel["required_bindings"].append(
        {"key": "custom", "label": "本地补充文件", "kind": "file"}
    )
    panel["required"][0]["sha256"] = "a" * 64
    state.document = document
    state.digest = catalog_digest(document)
    state.save(update_fields=["document", "digest"])

    call_command("migrate_resource_catalog", actor="pytest")

    panel = next(
        item
        for item in AnalysisResourceCatalog.objects.get(key="default").document["panels"]
        if item["id"] == "blood-84"
    )
    assert panel["bindings"]["bed"] == "hg38/custom/84-production.bed"
    assert panel["resource_version"] == "production-2026"
    assert panel["workflow_ids"] == [
        "custom-workflow",
        "tumor-blood-single-production",
        "tumor-blood-pair-production",
    ]
    assert any(item["key"] == "custom" for item in panel["required_bindings"])
    assert panel["required"][0]["sha256"] == "a" * 64

    version = AnalysisResourceCatalog.objects.get(key="default").version
    call_command("migrate_resource_catalog", actor="pytest")
    assert AnalysisResourceCatalog.objects.get(key="default").version == version


def test_resource_checks_sha256_and_bed_basename(settings, tmp_path):
    databases = tmp_path / "databases"
    bed = databases / "hg38/panels/targets.bed"
    bed.parent.mkdir(parents=True)
    bed.write_text("chr1\t1\t2\n", encoding="utf-8")
    settings.ANALYSIS_DATABASE_ROOT = databases
    entry = {
        "bindings": {"bed": "hg38/panels/targets.bed"},
        "required_bindings": [
            {
                "key": "bed",
                "label": "84 Panel BED",
                "kind": "file",
                "basename_includes": ["84panel"],
            }
        ],
        "required": [
            {
                "path": "hg38/panels/targets.bed",
                "label": "BED 内容",
                "kind": "file",
                "sha256": hashlib.sha256(b"different").hexdigest(),
            }
        ],
    }

    fast_requirements = entry_requirements(entry)
    assert all(item["reason"] != "checksum_mismatch" for item in fast_requirements)

    requirements = entry_requirements(entry, verify_checksums=True)

    reasons = {item["reason"] for item in requirements if not item["present"]}
    assert reasons == {"checksum_mismatch", "constraint_mismatch"}


def test_full_verification_returns_observed_directory_identity(settings, tmp_path):
    databases = tmp_path / "databases"
    bundle = databases / "hg19" / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "reference.fa").write_text(">chr1\nACGT\n", encoding="utf-8")
    settings.ANALYSIS_DATABASE_ROOT = databases
    entry = {
        "required": [
            {
                "path": "hg19/bundle",
                "label": "Reference bundle",
                "kind": "directory",
            }
        ]
    }

    requirement = entry_requirements(entry, verify_checksums=True)[0]

    assert requirement["present"] is True
    assert requirement["observed_identity_digest"].startswith("sha256:")


def test_missing_legacy_directory_is_not_reported_ready(settings, tmp_path):
    settings.ANALYSIS_DATABASE_ROOT = tmp_path / "databases"
    requirement = entry_requirements(
        {
            "required": [
                {
                    "path": "missing",
                    "label": "Missing directory",
                    "kind": "directory",
                    "sha256": "a" * 64,
                }
            ]
        }
    )[0]

    assert requirement["present"] is False
    assert requirement["reason"] == "missing"
    assert requirement["warning"] == "legacy_directory_sha256_ignored"


def test_catalog_preserves_legacy_directory_sha_and_explicit_identity_digest():
    document = {
        "schema_version": 1,
        "references": [
            {
                "id": "hg19",
                "name": "hg19",
                "required": [
                    {
                        "path": "hg19/bundle",
                        "kind": "directory",
                        "sha256": "a" * 64,
                        "identity_digest": "b" * 64,
                    }
                ],
            }
        ],
        "panels": [],
    }

    normalized = validate_catalog(document)

    assert normalized["references"][0]["required"][0]["sha256"] == "a" * 64
    assert (
        normalized["references"][0]["required"][0]["identity_digest"]
        == "b" * 64
    )


@pytest.mark.django_db
def test_resource_catalog_is_not_available_to_analysis_only_user(
    settings, django_user_model, resource_catalog_workspace
):
    settings.AUTH_REQUIRED = True
    user = django_user_model.objects.create_user(
        username="analysis-only", password="test-password"
    )
    group, _ = Group.objects.get_or_create(name="analysis-operators")
    user.groups.add(group)
    client_login = APIClient()
    logged_in = client_login.post(
        "/api/v1/auth/login",
        {"username": "analysis-only", "password": "test-password"},
        format="json",
    )
    assert logged_in.status_code == 200
    assert client_login.get("/api/v1/analysis/catalog").status_code == 200
    assert client_login.get("/api/v1/resource-catalog").status_code == 403

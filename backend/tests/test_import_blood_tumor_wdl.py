from pathlib import Path

import pytest
from django.core.management import call_command

from workflows.models import WDLAuditEvent, WDLAsset, WDLSourceRevision


SOURCE_DIR = Path(__file__).resolve().parents[2] / "test" / "血液肿瘤最新流程"


@pytest.mark.django_db
def test_import_blood_tumor_wdl_is_idempotent():
    options = {
        "source_dir": str(SOURCE_DIR),
        "repository": "fixture-repository",
        "revision": "",
        "actor": "zhuqin",
    }

    call_command("import_blood_tumor_wdl", **options)
    call_command("import_blood_tumor_wdl", **options)

    assets = WDLAsset.objects.filter(
        slug__in=[
            "tumor-blood-single-production",
            "tumor-blood-pair-production",
        ]
    ).prefetch_related("tags", "source_revisions__files")
    assert assets.count() == 2
    assert WDLSourceRevision.objects.filter(asset__in=assets).count() == 2
    assert WDLAuditEvent.objects.filter(asset__in=assets, action="import").count() == 2

    by_slug = {asset.slug: asset for asset in assets}
    single = by_slug["tumor-blood-single-production"]
    pair = by_slug["tumor-blood-pair-production"]
    assert single.source_revisions.get().files.count() == 2
    assert pair.source_revisions.get().files.count() == 2
    assert single.source_revision.startswith("sha256:")
    assert pair.source_revision.startswith("sha256:")
    assert {item.name for item in single.tags.all()} == {
        "血液肿瘤",
        "单样本",
        "hg38",
        "正式流程",
    }
    assert {item.name for item in pair.tags.all()} == {
        "血液肿瘤",
        "配对样本",
        "hg38",
        "正式流程",
    }

    single.name = "人工维护名称"
    single.description = "人工维护说明"
    single.save(update_fields=["name", "description"])
    single.tags.clear()
    call_command("import_blood_tumor_wdl", **options)
    single.refresh_from_db()
    assert single.name == "人工维护名称"
    assert single.description == "人工维护说明"
    assert single.tags.count() == 0

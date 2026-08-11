from django.db import migrations, models
from django.db.models.functions import Lower


def consolidate_case_insensitive_wdl_tags(apps, schema_editor):
    WDLTag = apps.get_model("workflows", "WDLTag")
    WDLAuditEvent = apps.get_model("workflows", "WDLAuditEvent")
    canonical_by_name = {}
    for tag in WDLTag.objects.order_by("id"):
        key = tag.name.lower()
        canonical = canonical_by_name.get(key)
        if canonical is None:
            canonical_by_name[key] = tag
            continue
        for asset in tag.wdl_assets.prefetch_related("tags"):
            before = [item.name for item in asset.tags.all()]
            asset.tags.add(canonical)
            asset.tags.remove(tag)
            after = [item.name for item in asset.tags.all()]
            asset.metadata_version += 1
            asset.save(update_fields=["metadata_version", "updated_at"])
            WDLAuditEvent.objects.create(
                asset=asset,
                action="metadata_update",
                actor="system-migration",
                note=f"合并重复标签 {tag.name} → {canonical.name}",
                changes={"tags": {"before": before, "after": after}},
            )
        tag.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0012_workflow_bundle_run_owner"),
    ]

    operations = [
        migrations.AddField(
            model_name="wdlasset",
            name="metadata_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(
            consolidate_case_insensitive_wdl_tags,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="wdltag",
            constraint=models.UniqueConstraint(
                Lower("name"),
                name="unique_wdl_tag_name_ci",
            ),
        ),
    ]

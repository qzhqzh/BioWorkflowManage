from django.db import migrations, models


def claim_seeded_workflows(apps, schema_editor):
    workflow_document = apps.get_model("workflows", "WorkflowDocument")
    workflow_document.objects.filter(
        slug__in=["fastp_bwa_demo", "fastp_demo", "fastp_qc_subflow"],
        created_by="local-user",
    ).update(created_by="zhuqin")


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0013_wdl_asset_metadata_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowdocument",
            name="created_by",
            field=models.CharField(default="local-user", max_length=256),
        ),
        migrations.RunPython(claim_seeded_workflows, migrations.RunPython.noop),
    ]

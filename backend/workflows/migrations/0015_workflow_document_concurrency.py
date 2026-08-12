from django.db import migrations, models


def backfill_updated_by(apps, schema_editor):
    workflow_document = apps.get_model("workflows", "WorkflowDocument")
    for document in workflow_document.objects.only("id", "created_by").iterator():
        workflow_document.objects.filter(pk=document.pk).update(
            updated_by=document.created_by
        )


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0014_workflow_document_created_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowdocument",
            name="document_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="workflowdocument",
            name="updated_by",
            field=models.CharField(default="local-user", max_length=256),
        ),
        migrations.RunPython(backfill_updated_by, migrations.RunPython.noop),
    ]
